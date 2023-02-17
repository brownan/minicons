"""Proof of concept of a dead simple dependency tracker and build framework"""
import json
import os
import shutil
import sqlite3
import stat
from abc import ABC, abstractmethod
from collections import defaultdict
from logging import getLogger
from pathlib import Path
from typing import (
    Any,
    Collection,
    Dict,
    Generic,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Protocol,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
)

ArgTypes = Union[Path, "Entry", "Builder", str]
Args = Union[ArgTypes, Iterable[ArgTypes]]
E = TypeVar("E", bound="Entry")
FileSources = Union[
    "File",
    str,
    Path,
    "Builder[File]",
]
FilesSources = Union[
    "File",
    "FileSet",
    "Dir",
    "Builder[File]",
    "Builder[FileSet]",
    "Builder[Dir]",
    str,
    Iterable[str],
]
DirSources = Union[
    "Dir",
    "Builder[Dir]",
]
FileSet = Iterable["File"]
BuilderType = TypeVar(
    "BuilderType",
    bound=Union[
        "File",
        "Dir",
        FileSet,
    ],
)




logger = getLogger("minicons")


class Dependable(Protocol):
    """An object that has dependencies"""

    depends: List["Entry"]


class DependencyError(Exception):
    pass


class Execution:
    def __init__(self) -> None:
        self.aliases: Dict[str, List[Path]] = {}

        # Maps paths to entries. Memoizes calls to file() and dir()
        # Path objects are always absolute
        self.entries: Dict[Path, "Entry"] = {}

        # Builders we have seen and have definitions for, and what files they build
        # This memoizes the get_targets() method of each builder
        self.builders: Dict["Builder", List["Entry"]] = {}

        self.metadata_db = sqlite3.connect(".miniscons.sqlite3", isolation_level=None)
        self.metadata_db.execute("""PRAGMA journal_mode=wal""")
        self.metadata_db.execute(
            """
        CREATE TABLE IF NOT EXISTS
        file_metadata (path text PRIMARY KEY, metadata text)
        """
        )

    def _get_metadata(self, path: Path) -> Dict[str, Any]:
        cursor = self.metadata_db.execute(
            """
        SELECT metadata FROM file_metadata WHERE path=?
        """,
            (str(path),),
        )
        row = cursor.fetchone()
        if not row:
            return {}
        return json.loads(row[0])

    def _set_metadata(self, path: Path, metadata: Dict[str, Any]) -> None:
        serialized = json.dumps(metadata)
        self.metadata_db.execute(
            """
        INSERT OR UPDATE INTO file_metadata (path, metadata) VALUES (?, ?)
        """,
            (str(path), serialized),
        )

    def _resolve_builder(self, builder: "Builder") -> List["Entry"]:
        """Resolves a builder to its output targets

        Also registers the builder with the environment
        """
        try:
            return self.builders[builder]
        except KeyError:
            pass
        # Register this builder and its targets
        builder_targets = builder.get_targets()
        entries: List["Entry"]
        if isinstance(builder_targets, Entry):
            entries = []
        else:
            entries = list(builder_targets)

        if any(isinstance(e, Dir) for e in entries) and len(entries) != 1:
            raise ValueError(
                f"Builder {builder} cannot output more than one target if "
                f"outputting a Directory"
            )

        for entry in entries:
            if not isinstance(entry, Entry):
                raise TypeError("Builder get_target() must return Files or Dirs")
            if entry.builder is not None and entry.builder is not builder:
                raise ValueError(f"{entry} is already being built by {entry.builder}")
            entry.builder = builder
            builder.builds.append(entry)
        self.builders[builder] = entries
        return entries

    def _args_to_paths(self, args: Args) -> Iterator[Path]:
        """Resolves a string, path, entry, or builder to Path objects

        Items may also be a list, possibly nested.

        Strings are interpreted as aliases if an alias exists, otherwise it is taken to
        be a path relative to the current working directory.

        """
        list_of_args: List[Args]
        if isinstance(args, (Path, Entry, Builder, str)):
            list_of_args = [args]
        else:
            list_of_args = list(args)

        for arg in list_of_args:
            if isinstance(arg, Entry):
                yield arg.path
            elif isinstance(arg, str):
                if arg in self.aliases:
                    yield from self._args_to_paths(self.aliases[arg])
                else:
                    yield Path.cwd().joinpath(arg)
            elif isinstance(arg, Builder):
                yield from self._args_to_paths(self._resolve_builder(arg))
            elif isinstance(arg, Path):
                yield arg
            elif isinstance(arg, Iterable):
                # Flatten the list
                yield from self._args_to_paths(arg)
            else:
                raise TypeError(f"Unknown argument type {arg}")

    def register_alias(self, alias: str, entries: Args) -> None:
        paths = list(self._args_to_paths(entries))
        self.aliases[alias] = paths

    def build_targets(self, targets: Args) -> None:
        # Resolve all targets to paths
        target_paths: List[Path] = list(self._args_to_paths(targets))

        # Resolve all paths to Entries
        try:
            target_entries: List["Entry"] = [self.entries[p] for p in target_paths]
        except KeyError as e:
            raise DependencyError(f"Target not found: {e}") from e

        # Traverse the graph of entry dependencies to get all entries relevant to this build
        # The dependency mapping returned contains not only the explicitly defined
        # entry->entry dependencies in Entry.depends, but also the dependencies
        # implied by the entry's builder's dependencies.
        all_entries, dependencies = _traverse_entry_graph(target_entries)

        # Get the topological ordering of the entries
        ordered_entries = _sort_dag(all_entries, dependencies)

        # Scan all entries to determine which are out of date
        out_of_date_entries: Set["Entry"] = set()
        for entry in ordered_entries:
            old_metadata = self._get_metadata(entry.path)
            if entry.path.exists():
                new_metadata = entry.get_metadata()
            else:
                new_metadata = None
            if old_metadata != new_metadata or new_metadata is None:
                out_of_date_entries.add(entry)

        # Evaluate entries
        built_entries: Set["Entry"] = set()
        for entry in ordered_entries:
            if (
                any(dep in out_of_date_entries for dep in dependencies[entry])
                and entry not in built_entries
            ):
                # One of this entry's dependencies is marked as out of date, so it must be
                # built.
                if not entry.builder:
                    raise DependencyError(
                        f"Dependency of {entry} is out of date, but no "
                        f"builder is defined"
                    )
                out_of_date_entries.add(entry)
                self._call_builder(entry.builder)
                built_entries.update(entry.builder.builds)

    def _call_builder(self, builder: "Builder") -> None:
        """Calls the given builder to build its entries"""
        logger.info("Building %s", builder)

        # First remove its entries and prepare them:
        for entry in builder.builds:
            entry.remove()
        for entry in builder.builds:
            entry.prepare()

        builder.build(self.builders[builder])

        # check this builder's output entries
        for out_entry in builder.builds:
            if not out_entry.path.exists():
                raise DependencyError(f"Builder {builder} didn't output {out_entry}")


def _traverse_entry_graph(
    targets: List["Entry"],
) -> Tuple[List["Entry"], Dict["Entry", List["Entry"]]]:
    """Given one or more target entries, traverse the graph of dependencies
    and return all reachable entries, as well as a mapping of dependency relations.

    """
    reachable_entries: List["Entry"] = []
    edges: Dict["Entry", List["Entry"]] = defaultdict(list)

    seen: Set[Entry] = set()
    to_visit = list(targets)
    while to_visit:
        visiting: Entry = to_visit.pop()
        reachable_entries.append(visiting)
        seen.add(visiting)

        dependencies = list(visiting.depends)
        if visiting.builder:
            dependencies.extend(visiting.builder.depends)

        for dep in dependencies:
            edges[visiting].append(dep)
            if dep not in seen:
                to_visit.append(dep)
    return reachable_entries, edges


def _sort_dag(
    nodes: Collection["Entry"], edges_orig: Mapping["Entry", Iterable["Entry"]]
) -> List["Entry"]:
    """Given a set of entries and a mapping describing the edges, returns a topological
    sort starting at the leaf nodes.

    Given edges are dependencies, so the topological sort is actually of the graph with
    all edges reversed. Leaf nodes are nodes with no dependencies.

    """
    # Copy the edges since we'll be mutating it
    edges: Dict["Entry", Set["Entry"]]
    edges = defaultdict(set, ((e, set(deps)) for e, deps in edges_orig.items()))

    # Create the reverse edges, or reverse dependencies (maps dependent nodes onto the
    # set of nodes that depend on it)
    reverse_edges: Dict["Entry", Set["Entry"]] = defaultdict(set)
    for e, deps in edges.items():
        for dep in deps:
            reverse_edges[dep].add(e)

    sorted_nodes: List[Entry] = []
    leaf_nodes: List["Entry"] = [n for n in nodes if not edges.get(n)]

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


current_execution: Optional[Execution] = None


def set_current_execution(e: Optional[Execution]) -> None:
    global current_execution
    current_execution = e


def get_current_execution() -> Execution:
    global current_execution
    execution = current_execution
    if not execution:
        raise RuntimeError("No current execution")
    return execution


def register_alias(alias: str, entries: Args) -> None:
    """Registers an alias with the current execution"""
    return get_current_execution().register_alias(alias, entries)


class Environment:
    def __init__(
        self, build_dir: Optional[Path] = None, execution: Optional[Execution] = None
    ):
        self.root = Path.cwd()
        self.build_root = build_dir or self.root.joinpath("build")

        if not execution:
            execution = get_current_execution()
        self.execution: Execution = execution

    def create_entry(
        self,
        path: Union[str, Path],
        factory: Type[E],
    ) -> E:
        # Make sure path is always absolute
        # Interpret relative to root
        path = self.root.joinpath(path)

        try:
            entry = self.execution.entries[path]
            if not isinstance(entry, factory):
                raise TypeError(f"Path {path} already exists but is the wrong type")
            return entry
        except KeyError:
            pass

        entry = factory(self, path)
        self.execution.entries[path] = entry
        return entry

    def file(
        self,
        path: Union[str, Path, "File"],
    ) -> "File":
        if isinstance(path, File):
            return path
        return self.create_entry(path, File)

    def dir(
        self,
        path: Union[str, Path, "Dir"],
    ) -> "Dir":
        if isinstance(path, Dir):
            return path
        return self.create_entry(
            path,
            Dir,
        )

    def get_rel_path(self, src: Union[str, Path]) -> str:
        src = self.root.joinpath(src)

        # See if the build directory is one of the parents:
        try:
            index = src.parents.index(self.build_root)
        except ValueError:
            # Result is relative to the root
            rel_path = src.relative_to(self.root)
        else:
            # Result is relative to the directory under the build root
            rel_path = src.relative_to(src.parents[index - 1])

        return str(rel_path)

    def get_build_path(
        self,
        src: Union[str, Path],
        build_dir: Union[str, Path],
        new_ext: Optional[str] = None,
    ) -> Path:
        rel_path = self.get_rel_path(src)
        build_dir = self.build_root.joinpath(build_dir)

        full_path = build_dir.joinpath(rel_path)
        if new_ext is not None:
            full_path = full_path.with_suffix(new_ext)
        return full_path

    def depends_file(self, target: "Builder", source: FileSources) -> "File":
        """Register the given source as a dependency of the given builder

        Resolves the given source object and returns a File object

        """
        file: "File"
        if isinstance(source, Builder):
            b_targets = self.execution._resolve_builder(source)
            files = [t for t in b_targets if isinstance(t, File)]
            if len(files) != 1:
                raise ValueError(f"Builder {source} expected to return a single file")
            file = files[0]
        else:
            file = self.file(source)

        target.depends.append(file)
        return file

    def depends_files(
        self,
        target: "Builder",
        sources: FilesSources,
    ) -> "FileSet":
        """Register the given source(s) as depenedncies of the given builder

        Resolves the given source(s) and returns some object that, when iterated over,
        produces File objects. This is typically either a list of Files or a Dir.

        """
        if isinstance(sources, Builder):
            b_targets = self.execution._resolve_builder(sources)
            if len(b_targets) == 1:
                d = b_targets[0]
                if isinstance(d, Dir):
                    target.depends.append(d)
                    return d
            # isinstance filter is just a sanity check at this point. Builders
            # should not be returning multiple objects unless they're all File objects.
            # This limitation could possibly be removed if needed.
            files = [t for t in b_targets if isinstance(t, File)]
            target.depends.extend(files)
            return files
        elif isinstance(sources, Dir):
            target.depends.append(sources)
            return sources
        elif isinstance(sources, File):
            target.depends.append(sources)
            return [sources]
        elif isinstance(sources, str):
            file = self.file(sources)
            target.depends.append(file)
            return [file]
        else:
            files = [self.file(s) for s in sources]
            target.depends.extend(files)
            return files

    def depends_dir(self, target: "Builder", source: DirSources) -> "Dir":
        """Register the given source as a dependency of the given target builder

        This is specifically when the caller is expecting a directory.

        """
        if isinstance(source, Builder):
            b_targets = self.execution._resolve_builder(source)
            if len(b_targets) != 1:
                raise ValueError(
                    f"Builder {source} expected to return exactly one directory"
                )
            d = b_targets[0]
            if not isinstance(d, Dir):
                raise ValueError(
                    f"Builder {source} expected to return exactly one directory"
                )
        else:
            d = self.dir(source)

        target.depends.append(d)
        return d


class Entry(ABC):
    """Represents a file or a directory on the filesystem"""

    def __init__(
        self,
        env: "Environment",
        path: Path,
    ):
        self.env = env
        self.path: Path = path

        # Which builder builds this entry
        # That builder's dependencies are implicit dependencies of this entry.
        self.builder: Optional["Builder"] = None

        # Explicit list of additional other entries this one depends on
        self.depends: List["Entry"] = []

    def __hash__(self) -> int:
        return hash(self.path)

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, type(self)) and self.path == other.path

    def __str__(self) -> str:
        try:
            return str(self.path.relative_to(self.env.root))
        except ValueError:
            return str(self.path)

    def __repr__(self) -> str:
        cls_name = self.__class__.__name__
        rel_path = str(self)
        return f"{cls_name}({rel_path!r})"

    def derive(self: E, build_dir_name: str, new_ext: Optional[str] = None) -> E:
        """Create a derivative file/dir from this entry"""
        new_path = self.env.get_build_path(self.path, build_dir_name, new_ext)

        return self.env.create_entry(new_path, type(self))

    def prepare(self) -> None:
        """Hook for the entry to do anything it may need to do before being built

        Called right before its builder is called.
        """
        # Make sure the parent directory exists
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @abstractmethod
    def get_metadata(self) -> Dict[str, Any]:
        ...

    @abstractmethod
    def remove(self) -> None:
        ...


class File(Entry):
    def get_metadata(self) -> Dict[str, Any]:
        stat_result = os.stat(self.path)
        return {
            "mtime": stat_result.st_mtime,
            "is_file": stat.S_ISREG(stat_result.st_mode),
        }

    def remove(self) -> None:
        self.path.unlink(missing_ok=True)


class Dir(Entry):
    def __iter__(self) -> Iterator["File"]:
        for path in self.path.glob("**/*"):
            if path.is_file():
                yield self.env.file(path)

    def get_metadata(self) -> Dict[str, Any]:
        stat_result = os.stat(self.path)
        metadata: Dict[str, Any]
        metadata = {"is_dir": stat.S_ISDIR(stat_result.st_mode), "files": {}}

        file_list: List["File"] = list(self)
        for file in file_list:
            file_metadata = file.get_metadata()
            metadata["files"][str(file.path)] = file_metadata
        return metadata

    def remove(self) -> None:
        if self.path.is_dir():
            shutil.rmtree(self.path)


class Builder(ABC, Generic[BuilderType]):
    def __init__(self, env: "Environment"):
        self.env = env

        # Which other entries this builder depends on
        # These dependencies are resolved at build time. Conceptually this translates to
        # all of this builder's output (target) entries depend on each entry in this list.
        self.depends: List["Entry"] = []

        # List of items this builder builds. It is populated automatically by the results
        # from get_targets() and any calls to side_effects()
        self.builds: List["Entry"] = []

    def __str__(self) -> str:
        return "{}({})".format(type(self).__name__, " ".join(str(b) for b in self.builds))

    def side_effect(self, entries: Union["Entry", Iterable["Entry"]]) -> None:
        """Registers additional entries as outputs of the current builder, in addition
        to the files the builder returned from get_targets()

        Builders should call this to declare additional files they output.

        """
        if isinstance(entries, Entry):
            entries = [entries]
        for entry in entries:
            if entry.builder and entry.builder is not self:
                raise ValueError(f"{entry} is already being built by {entry.builder}")
            entry.builder = self
            self.builds.append(entry)

    @abstractmethod
    def get_targets(self) -> BuilderType:
        """Returns a File, list of Files, or a Directory declaring what this builder outputs

        Abstract files / directories are resolved to concrete files by the dependent
        builders before passing back to this builder's build().

        The returned items from this method declare what this builder outputs. Generally,
        builders should output abstract files if the exact set of files is known at resolution
        time.

        Some builders don't know the set of files being output until build time, in which case
        they should return an abstract directory which the builder should use at build time
        to write its output files to.

        If the builder wants to output its files to a particular place in the filesystem,
        it can return a concrete file list or directory. The intent of abstract files
        is so builders don't have to worry about the location of their output, letting
        the build framework determine where files should go.

        """

    @abstractmethod
    def build(self, targets: BuilderType) -> None:
        """Called to actually build the targets

        Output should go to the given targets.
        The targets argument is a concrete or set of concrete objects corresponding to
        what was previously returned by get_output().

        So if get_output() returned an AbstractFile, this method is passed a ConcreteFile
        and is expected to write its output to that file.

        Similarly, if AbstractDirectory was given, a ConcreteDirectory is passed in here
        and this builder is expected to write all its files underneath the given directory.
        """
