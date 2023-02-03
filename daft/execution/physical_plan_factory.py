from __future__ import annotations

from typing import Iterator, TypeVar

from daft.execution import execution_step, physical_plan
from daft.execution.execution_step import ExecutionStep, MaterializationRequestBase
from daft.logical import logical_plan
from daft.logical.logical_plan import LogicalPlan, PartitionScheme

PartitionT = TypeVar("PartitionT")


def get_materializing_physical_plan(
    node: LogicalPlan, psets: dict[str, list[PartitionT]]
) -> Iterator[None | MaterializationRequestBase[PartitionT]]:
    """Translates a LogicalPlan into an appropriate physical plan that materializes its final results."""

    return physical_plan.materialize(get_physical_plan(node, psets))


def get_physical_plan(
    node: LogicalPlan, psets: dict[str, list[PartitionT]]
) -> Iterator[None | ExecutionStep[PartitionT]]:
    """Translates a LogicalPlan into an appropriate physical plan.

    See physical_plan.py for more details.
    """

    # -- Leaf nodes. --
    if isinstance(node, logical_plan.InMemoryScan):
        partitions = psets[node._cache_entry.key]
        return physical_plan.partition_read(_ for _ in partitions)

    # -- Unary nodes. --
    elif isinstance(node, logical_plan.UnaryNode):
        [child_node] = node._children()
        child_plan: Iterator[None | ExecutionStep[PartitionT]] = get_physical_plan(child_node, psets)

        if isinstance(node, logical_plan.TabularFilesScan):
            return physical_plan.file_read(child_plan=child_plan, scan_info=node)

        elif isinstance(node, logical_plan.Filter):
            return physical_plan.pipeline_instruction(
                child_plan=child_plan,
                pipeable_instruction=execution_step.Filter(node._predicate),
                resource_request=node.resource_request(),
            )

        elif isinstance(node, logical_plan.Projection):
            return physical_plan.pipeline_instruction(
                child_plan=child_plan,
                pipeable_instruction=execution_step.Project(node._projection),
                resource_request=node.resource_request(),
            )

        elif isinstance(node, logical_plan.MapPartition):
            return physical_plan.pipeline_instruction(
                child_plan=child_plan,
                pipeable_instruction=execution_step.MapPartition(node._map_partition_op),
                resource_request=node.resource_request(),
            )

        elif isinstance(node, logical_plan.LocalAggregate):
            return physical_plan.pipeline_instruction(
                child_plan=child_plan,
                pipeable_instruction=execution_step.Aggregate(to_agg=node._agg, group_by=node._group_by),
                resource_request=node.resource_request(),
            )

        elif isinstance(node, logical_plan.LocalDistinct):
            return physical_plan.pipeline_instruction(
                child_plan=child_plan,
                pipeable_instruction=execution_step.Aggregate(to_agg=[], group_by=node._group_by),
                resource_request=node.resource_request(),
            )

        elif isinstance(node, logical_plan.FileWrite):
            return physical_plan.file_write(child_plan, node)

        elif isinstance(node, logical_plan.LocalLimit):
            # Note that the GlobalLimit physical plan also dynamically dispatches its own LocalLimit instructions.
            return physical_plan.local_limit(child_plan, node._num)

        elif isinstance(node, logical_plan.GlobalLimit):
            return physical_plan.global_limit(child_plan, node)

        elif isinstance(node, logical_plan.Repartition):
            # Translate PartitionScheme to the appropriate fanout instruction.
            fanout_instruction: execution_step.FanoutInstruction
            if node._scheme == PartitionScheme.RANDOM:
                fanout_instruction = execution_step.FanoutRandom(num_outputs=node.num_partitions())
            elif node._scheme == PartitionScheme.HASH:
                fanout_instruction = execution_step.FanoutHash(
                    num_outputs=node.num_partitions(),
                    partition_by=node._partition_by,
                )
            else:
                raise RuntimeError(f"Unimplemented partitioning scheme {node._scheme}")

            # Do the fanout.
            fanout_plan = physical_plan.pipeline_instruction(
                child_plan=child_plan,
                pipeable_instruction=fanout_instruction,
                resource_request=node.resource_request(),
            )

            # Do the reduce.
            return physical_plan.reduce(
                fanout_plan=fanout_plan,
                num_partitions=node.num_partitions(),
                reduce_instruction=execution_step.ReduceMerge(),
            )

        elif isinstance(node, logical_plan.Sort):
            return physical_plan.sort(child_plan, node)

        elif isinstance(node, logical_plan.Coalesce):
            return physical_plan.coalesce(child_plan, node)

        else:
            raise NotImplementedError(f"Unsupported plan type {node}")

    # -- Binary nodes. --
    elif isinstance(node, logical_plan.BinaryNode):
        [left_child, right_child] = node._children()

        if isinstance(node, logical_plan.Join):
            return physical_plan.join(
                left_plan=get_physical_plan(left_child, psets),
                right_plan=get_physical_plan(right_child, psets),
                join=node,
            )

        else:
            raise NotImplementedError(f"Unsupported plan type {node}")

    else:
        raise NotImplementedError(f"Unsupported plan type {node}")