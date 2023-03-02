import concurrent.futures
import dataclasses
import json
import os
import sqlite3
from collections import defaultdict
from logging import getLogger
from pathlib import Path
from typing import (
    AbstractSet,
    Any,
    Collection,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

from minicons.builder import Builder
from minicons.entry import Entry, Node
from minicons.types import Args

logger = getLogger("minicons")


class DependencyError(Exception):
    pass


@dataclasses.dataclass
class PreparedBuild:
    ordered_nodes: Sequence[Node]
    buildable_entries: Set[Entry]
    edges: Mapping[Node, Collection[Node]]
    out_of_date: Collection[Entry]
    changed: Collection[Node]
    entry_dependencies: Mapping[Node, Collection[Entry]]
    targets: Sequence[Node]

    def get_to_build(self) -> AbstractSet[Node]:
        """From the collection of out-of-date entries, calculate and return the full set
        of nodes that need building, (including nodes that don't have a builder)

        The returned set includes out-of-date entries, and also any nodes which need to
        be (re)built as a consequence of building the out-of-date entries.

        """
        # Nodes that are outdated and need rebuilding also imply their descendent nodes should be
        # rebuilt. While such nodes are usually also detected as outdated, they may
        # not be in the case of an interrupted build where e.g. one dependent node
        # got built but not another. Since we don't assume a builder is purely functional,
        # if a builder runs, all descendent builders are also run.
        # Also since non-entry nodes are never detected directly as out-of-date, this is
        # where those nodes are set to build.
        to_build: Set[Node] = set(self.out_of_date)
        for node in self.ordered_nodes:
            if any(d in to_build for d in self.edges[node]):
                to_build.add(node)

        # Nodes which depend on a non-Entry node require that node to be rebuilt.
        # This is because a non-Entry node's contents are not defined until its builder
        # builds it. For example, a FileSet is used to contain a set of files not known
        # statically. So its builder must build it before it can be used by a downstream
        # dependent builder.
        # Traverse the graph /upward/ so that nodes depending on other nodes propagate
        # correctly. (A FileSet whose builder depends on another FileSet requires
        # both to be built)
        for node in reversed(self.ordered_nodes):
            if node in to_build:
                to_build.update(d for d in self.edges[node] if not isinstance(d, Entry))

        return to_build


class Execution(MutableMapping[str, Any]):
    """An execution is the top level object which controls the build process

    An execution object keeps track of all the builders, entries, and aliases. Environments
    attached to an Execution will add their builders and entries to their Execution instance.

    Typically, a process will have a single global Execution instance, but for embedding minicons
    in a larger application it may be useful to manage separate Executions.
    """

    def __init__(self, root: Union[str, Path]) -> None:
        self.root = Path(root).resolve()
        self.aliases: Dict[str, List[Node]] = {}

        # Maps paths to entries. Memoizes calls to file() and dir(), and is used to
        # lookup and resolve target paths given as strings to entries.
        # Path objects are always absolute
        self.entries: Dict[Path, "Entry"] = {}

        self.metadata_db = sqlite3.connect(
            self.root.joinpath(".minicons.sqlite3"), isolation_level=None
        )
        self.metadata_db.execute("""PRAGMA journal_mode=wal""")
        self.metadata_db.execute(
            """
            CREATE TABLE IF NOT EXISTS
            file_metadata (path text PRIMARY KEY, metadata text)
            """
        )

        self._env_vars: Dict[str, Any] = {}

    def __getitem__(self, item: str) -> Any:
        return self._env_vars[item]

    def __setitem__(self, key: str, value: Any) -> None:
        self._env_vars[key] = value

    def __delitem__(self, key: str) -> None:
        del self._env_vars[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._env_vars)

    def __len__(self) -> int:
        return len(self._env_vars)

    def _get_metadata(self, path: Path) -> Optional[Dict[str, Any]]:
        cursor = self.metadata_db.execute(
            """
        SELECT metadata FROM file_metadata WHERE path=?
        """,
            (str(path),),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return json.loads(row[0])

    def _set_metadata(self, path: Path, metadata: Dict[str, Any]) -> None:
        serialized = json.dumps(metadata)
        self.metadata_db.execute(
            """
            INSERT OR REPLACE INTO file_metadata (path, metadata) VALUES (?, ?)
            """,
            (str(path), serialized),
        )

    def _args_to_nodes(self, args: Args) -> Iterator[Node]:
        """Resolves a string, path, Node, or SourceLike to Node objects

        Items may also be an Iterable, possibly nested.

        Strings are interpreted as aliases if an alias exists, otherwise it is taken to
        be a path relative to the current working directory.

        """
        if isinstance(args, Path):
            yield self.entries[args]
        elif isinstance(args, Node):
            yield args
        elif isinstance(args, str):
            # try interpreting this as an alias first, then as a path
            if args in self.aliases:
                yield from self._args_to_nodes(self.aliases[args])
            else:
                yield self.entries[self.root.joinpath(args)]
        elif hasattr(args, "target"):
            yield from self._args_to_nodes(args.target)
        elif isinstance(args, Iterable):
            for item in args:
                yield from self._args_to_nodes(item)
        else:
            raise TypeError(f"Unknown arg type {args!r}")

    def register_alias(self, alias: str, entries: Args) -> None:
        """Register a new alias that can be used on the command line to refer to one or
        more filesystem entries

        """
        nodes = list(self._args_to_nodes(entries))
        self.aliases[alias] = nodes

    def prepare_build(self, targets: Args) -> PreparedBuild:
        """Prepare to build the given targets

        This builds the final dependency graph and the set of out of date nodes
        """
        # Resolve all targets to paths
        target_nodes: List[Node] = list(self._args_to_nodes(targets))

        # Traverse the graph of entry dependencies to get all entries relevant to this build
        # The dependency mapping returned contains not only the explicitly defined
        # entry->entry dependencies in Entry.depends, but also the dependencies
        # implied by the entry's builder's dependencies.
        all_nodes, edges = _traverse_node_graph(target_nodes)

        # Get the topological ordering of the entries
        ordered_nodes: Sequence[Node] = _sort_dag(all_nodes, edges)

        # Entry nodes are nodes with a statically defined filesystem path known before
        # calling its builder. Most operations below only apply to entry nodes. In particular,
        # because the cached metadata is keyed off of the filesystem path. Non-entry
        # nodes (such as FileSet nodes) are treated differently because they can't have
        # saved metadata and must be built before they can be used.
        entry_nodes: Set[Entry] = {node for node in all_nodes if isinstance(node, Entry)}

        # For each node, walk the graph towards leaf nodes to record all nodes this one
        # depends on -- all ancestor nodes.
        all_dependencies: Dict[Node, Set[Entry]] = {}
        for node in all_nodes:
            all_dependencies[node] = set()
            to_visit = list(edges[node])
            while to_visit:
                v = to_visit.pop()
                if isinstance(v, Entry):
                    all_dependencies[node].add(v)
                to_visit.extend(edges[v])

        # Gather filesystem metadata on all nodes now for use in comparisons in the next step.
        metadata: Dict[Entry, Any] = {}
        for entry in entry_nodes:
            if entry.builder is None and not entry.path.exists():
                raise DependencyError(
                    f"Path {entry} required but not present on filesystem and no builder defined"
                )
            metadata[entry] = entry.get_metadata()

        # Each node has stored metadata on every other node it depends on, which forms a signature
        # to determine if that nodes needs rebuilding. Use the gathered metadata above along
        # with the node's all_dependencies set, to compare its metadata to the cached copy.
        outdated: Set[Entry] = set()
        changed: Set[Entry] = set()
        for node in all_nodes:
            if isinstance(node, Entry) and node.builder is not None:
                if not node.path.exists():
                    # If the node doesn't exist, then of course it needs building.
                    outdated.add(node)
                else:
                    old_metadata = self._get_metadata(node.path)
                    new_metadata = self._metadata_signature(
                        metadata, all_dependencies[node]
                    )
                    if old_metadata != new_metadata:
                        outdated.add(node)
                        for dep in all_dependencies[node]:
                            path = str(dep.path)
                            if old_metadata is not None and old_metadata.get(
                                path
                            ) != new_metadata.get(path):
                                changed.add(dep)

        return PreparedBuild(
            ordered_nodes=ordered_nodes,
            buildable_entries={
                e for e in ordered_nodes if isinstance(e, Entry) and e.builder is not None
            },
            out_of_date=outdated,
            edges=edges,
            entry_dependencies=all_dependencies,
            targets=target_nodes,
            changed=changed,
        )

    def build_targets(
        self,
        targets: Optional[Args] = None,
        prepared_build: Optional[PreparedBuild] = None,
        dry_run: bool = False,
        parallel: Union[bool, int] = False,
    ) -> None:
        """Build the given targets

        A target or list of targets is given to build. Alternatively, a PreparedBuild as previously
        returned from Execution.prepare_build() may be given.

        """
        if prepared_build is None:
            if targets is None:
                raise ValueError("Either targets or a prepared build must be given")
            else:
                prepared_build = self.prepare_build(targets)
        elif targets is not None:
            raise ValueError("Targets and prepared_build cannot be specified together")

        if parallel is True:
            parallel = os.cpu_count() or 1
        elif parallel is False:
            parallel = 1

        ordered_nodes = prepared_build.ordered_nodes
        entry_dependencies = prepared_build.entry_dependencies
        to_build = prepared_build.get_to_build()

        if not to_build:
            logger.info("All files up to date")
            return

        executor: Optional[concurrent.futures.Executor]
        if parallel > 1 and not dry_run:
            executor = concurrent.futures.ThreadPoolExecutor(parallel)
        else:
            executor = None

        # As nodes are built, metadata on their dependencies is gathered and saved here, so
        # it can be reused between multiple nodes that depend on the same file.
        metadata_cache: Dict[Entry, Any] = {}

        # Start the build process
        if executor is None:
            built_nodes: Set[Node] = set()
            for node in ordered_nodes:
                if (
                    node in to_build
                    and node not in built_nodes
                    and node.builder is not None
                ):
                    builder = node.builder
                    self._call_builder(builder, dry_run)
                    built_nodes.update(builder.builds)
                    if not dry_run:
                        self._update_builder_metadata(
                            builder, entry_dependencies, metadata_cache
                        )
        else:
            # The strategy here is a bit different. Instead of simply executing nodes
            # in topological order, we execute any nodes which have all their dependencies
            # built. This way we can execute separate paths of the DAG simultaneously.
            edges: Dict[Node, Set[Node]]
            edges = {node: set(deps) for node, deps in prepared_build.edges.items()}
            reverse_edges: Dict[Node, Set[Node]] = defaultdict(set)
            for node, deps in edges.items():
                for dep in deps:
                    reverse_edges[dep].add(node)

            # Remove from the graph all nodes that don't need building
            for node in ordered_nodes:
                if node not in to_build:
                    for n in list(edges.get(node) or ()):
                        reverse_edges[n].remove(node)
                        edges[node].remove(n)

                    for n in list(reverse_edges.get(node) or ()):
                        edges[n].remove(node)
                        reverse_edges[node].remove(n)

            # Nodes that don't have any dependencies are ready to be built
            ready_to_build: Set[Node] = {node for node in to_build if not edges[node]}

            def node_built(n: Node) -> None:
                nonlocal to_build
                # Remove this node from the dependency graph. If any child nodes
                # are now leaves, add them to the ready_to_build set.
                children = list(reverse_edges[n])
                for child in children:
                    edges[child].remove(n)
                    reverse_edges[n].remove(child)
                    if not edges[child]:
                        ready_to_build.add(child)

            futures: Set[concurrent.futures.Future] = set()
            try:
                while True:
                    if ready_to_build:
                        node = ready_to_build.pop()
                        if node.builder is None:
                            # This only applies to filesets. Entries without builders
                            # are removed already, so this assert would indicate a bug
                            assert not isinstance(node, Entry)
                            node_built(node)
                            continue

                        builder = node.builder
                        logger.debug("Submitting builder job: %s", builder)
                        ready_to_build.difference_update(builder.builds)
                        futures.add(executor.submit(self._call_builder, builder, dry_run))
                    elif futures:
                        done, futures = concurrent.futures.wait(
                            futures, return_when=concurrent.futures.FIRST_COMPLETED
                        )
                        f: concurrent.futures.Future
                        for f in done:
                            builder = f.result()
                            if not dry_run:
                                self._update_builder_metadata(
                                    builder, entry_dependencies, metadata_cache
                                )

                            for node in builder.builds:
                                node_built(node)
                    else:
                        # Nothing is executing and nothing is ready to build
                        break
            finally:
                for f in futures:
                    f.cancel()
                logger.debug(
                    "Canceled %s futures and shutting down executor...", len(futures)
                )
                executor.shutdown()

            remaining_nodes = set(node for node, deps in edges.items() if deps)
            if remaining_nodes:
                raise RuntimeError(
                    f"Error resolving dependency graph. Nodes didn't execute: {remaining_nodes}"
                )

    def _call_builder(
        self,
        builder: Builder,
        dry_run: bool,
    ) -> Builder:
        """Calls the given builder to build its entries"""
        # First remove its entries and prepare them:
        if not dry_run:
            for entry in builder.builds:
                if isinstance(entry, Entry):
                    logger.debug(f"Removing {entry}")
                    entry.remove()
            for entry in builder.builds:
                if isinstance(entry, Entry):
                    entry.prepare()

        # Log the build message even if we're in dry-run mode. (The point is to see what
        # would build)
        logger.info(str(builder))

        if not dry_run:
            builder.build()

            # check that the outputs were actually created
            for entry in builder.builds:
                if isinstance(entry, Entry) and not entry.path.exists():
                    raise DependencyError(f"Builder {builder} didn't output {entry}")

        return builder

    def _update_builder_metadata(
        self,
        builder: Builder,
        entry_dependencies: Mapping[Node, Collection[Entry]],
        metadata_cache: Dict[Entry, Any],
    ) -> None:
        for built_entry in builder.builds:
            if isinstance(built_entry, Entry):
                # Before updating this new entry's metadata, gather the
                # individual file metadata for all its dependencies
                for dep in entry_dependencies[built_entry]:
                    if dep not in metadata_cache:
                        metadata_cache[dep] = dep.get_metadata()
                # Now update this entry's cached metadata
                self._set_metadata(
                    built_entry.path,
                    self._metadata_signature(
                        metadata_cache, entry_dependencies[built_entry]
                    ),
                )

    def _metadata_signature(
        self,
        metadata: Mapping[Entry, Any],
        nodes: Iterable[Entry],
    ) -> Dict:
        return {str(e.path): metadata[e] for e in nodes}


def _traverse_node_graph(
    targets: List["Node"],
) -> Tuple[List["Node"], Dict["Node", List["Node"]]]:
    """Given one or more target nodes, traverse the graph of dependencies
    and return all reachable nodes, as well as a mapping of dependency relations.

    """
    reachable_entries: List["Node"] = []
    edges: Dict["Node", List["Node"]] = defaultdict(list)

    seen: Set[Node] = set()
    to_visit = list(targets)
    while to_visit:
        visiting: Node = to_visit.pop()
        reachable_entries.append(visiting)
        seen.add(visiting)

        # A node depends on both its explicit dependencies and also its builder's dependencies
        dependencies = set(visiting.depends)
        if visiting.builder:
            dependencies.update(visiting.builder.depends)

            # A node ALSO implicitly depends on all its siblings' dependencies (sibling nodes
            # being nodes built by the same builder).
            # Say a node and its builder each have no dependencies, but the builder outputs another
            # node which DOES have dependencies. Those dependencies must be built before the
            # builder is run (and the no-dependency node is built)
            # This can happen if a builder builds a fileset, and the fileset contains nodes
            # which themselves have dependencies.
            for sibling in visiting.builder.builds:
                dependencies.update(sibling.depends)

        for dep in dependencies:
            edges[visiting].append(dep)
            if dep not in seen:
                to_visit.append(dep)
    return reachable_entries, edges


def _sort_dag(
    nodes: Collection["Node"], edges_orig: Mapping["Node", Iterable["Node"]]
) -> List["Node"]:
    """Given a set of nodes and a mapping describing the edges, returns a topological
    sort starting at the leaf nodes.

    Given edges are dependencies, so the topological sort is actually of the graph with
    all edges reversed. Leaf nodes are nodes with no dependencies.

    """
    # Copy the edges since we'll be mutating it
    edges: Dict["Node", Set["Node"]]
    edges = defaultdict(set, ((e, set(deps)) for e, deps in edges_orig.items()))

    # Create the reverse edges, or reverse dependencies (maps dependent nodes onto the
    # set of nodes that depend on it)
    reverse_edges: Dict["Node", Set["Node"]] = defaultdict(set)
    for e, deps in edges.items():
        for dep in deps:
            reverse_edges[dep].add(e)

    sorted_nodes: List[Node] = []
    leaf_nodes: List[Node] = [n for n in nodes if not edges.get(n)]

    while leaf_nodes:
        node = leaf_nodes.pop()
        sorted_nodes.append(node)
        for m in list(reverse_edges[node]):
            reverse_edges[node].remove(m)
            edges[m].remove(node)
            if not edges[m]:
                leaf_nodes.append(m)

    if any(deps for deps in edges.values()):
        msg = "\n".join(
            f"{n} → {dep}" for n, deps in edges.items() if deps for dep in deps
        )
        raise DependencyError(f"Dependency graph has cycles:\n{msg}")

    return sorted_nodes


def set_current_execution(e: Optional[Execution]) -> None:
    global current_execution
    current_execution = e


def get_current_execution() -> Execution:
    global current_execution
    execution = current_execution
    if execution is None:
        raise RuntimeError("No current execution")
    return execution


def register_alias(alias: str, entries: Args) -> None:
    """Registers an alias with the current execution"""
    return get_current_execution().register_alias(alias, entries)


current_execution: Optional[Execution] = None
