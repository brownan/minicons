import argparse
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

import minicons.execution
from minicons import Dir, Entry, File, FileSet, Node
from minicons.execution import PreparedBuild


class TreeAction(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Union[str, Sequence[Any], None],
        option_string: Optional[str] = None,
    ) -> None:
        if option_string and option_string.endswith("=all"):
            namespace.tree = "all"
        else:
            namespace.tree = True


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--construct", default="construct.py")
    parser.add_argument("-B", "--always-build", action="store_true")
    parser.add_argument("-d", "--dry-run", action="store_true")
    parser.add_argument("--tree", "--tree=all", action=TreeAction, nargs=0)
    parser.add_argument("target", nargs="+")
    args = vars(parser.parse_args())

    print(args["tree"])

    construct_path = Path(args["construct"]).resolve()
    contents = open(construct_path, "r").read()
    code = compile(contents, args["construct"], "exec", optimize=0)

    current_execution = minicons.execution.Execution(construct_path.parent)
    minicons.execution.set_current_execution(current_execution)
    try:
        exec(code, {})
    finally:
        minicons.execution.set_current_execution(None)

    prepared = current_execution.prepare_build(args["target"])

    if args["always_build"]:
        prepared.to_build = set(
            n for n in prepared.ordered_nodes if n.builder is not None
        )

    if args["tree"]:
        print_tree(prepared, all_nodes=args["tree"] == "all")

    current_execution.build_targets(prepared_build=prepared, dry_run=args["dry_run"])


def print_tree(
    build: PreparedBuild,
    all_nodes: bool = False,
) -> None:
    targets = build.targets
    ordered_nodes = build.ordered_nodes
    edges = build.edges
    out_of_date = build.out_of_date
    to_build = build.to_build
    changed = build.changed

    new_edges: Dict[Node, List[Node]] = {e: list(d) for e, d in edges.items()}
    if not all_nodes:
        # traverse the graph forming a new graph that eliminates non-entry nodes
        for node in ordered_nodes:
            for child in list(new_edges[node]):
                if not isinstance(child, Entry):
                    # Remove this edge
                    new_edges[node].remove(child)
                    # Add new edges to children
                    new_edges[node].extend(new_edges[child])

    seen: Set[Node] = set()
    to_visit: List[Tuple[Node, List[bool], bool]] = list((t, [], False) for t in targets)

    # Nodes are popped off the end of the list. So that we print them in the original order,
    # reverse this list.
    to_visit.reverse()

    print("O = out of date")
    print("B = to build")
    print("C = changed")

    # Now walk this new graph printing out nodes as found, keeping track of the depth.
    while to_visit:
        node, depth_seq, last_child = to_visit.pop()
        skip_children = bool(node in seen and new_edges[node])

        if not depth_seq:
            print()

        _print_line(
            node in out_of_date,
            node in to_build,
            node in changed,
            depth_seq,
            last_child,
            str(node),
            skip_children,
        )

        if not skip_children:
            seen.add(node)
            children = list(new_edges[node])
            children_set = set(children)

            # Show directories first, then files. Secondary sort by name
            children.sort(
                key=lambda node: ((FileSet, Dir, File).index(type(node)), str(node)),
                reverse=True,
            )
            for child in children:
                if child in children_set:
                    if not depth_seq:
                        new_depth_seq = [False]
                    else:
                        new_depth_seq = depth_seq[:-1] + [not last_child, True]
                    to_visit.append((child, new_depth_seq, child is children[0]))
                    children_set.remove(child)


def _print_line(
    out_of_date: bool,
    to_build: bool,
    changed: bool,
    depth_seq: List[bool],
    last_child: bool,
    name: str,
    omit_children: bool,
) -> None:
    parts = []
    parts.append(
        "{} {} {} ".format(
            "O" if out_of_date else " ",
            "B" if to_build else " ",
            "C" if changed else " ",
        )
    )

    if depth_seq:
        parts.append(" ")
        for d in depth_seq[:-1]:
            if d:
                parts.append("│  ")
            else:
                parts.append("   ")

        if last_child:
            parts.append("└─")
        else:
            parts.append("├─")

    parts.append(name)

    print("".join(parts))
    if omit_children:
        _print_line(
            False,
            False,
            False,
            depth_seq + [depth_seq[-1]],
            True,
            "(child nodes shown above)",
            False,
        )


if __name__ == "__main__":
    main()
