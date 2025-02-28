import torch
import torch.distributed as dist
from typing import Any, Callable


def _allreduce_fut(
    process_group: dist.ProcessGroup, tensor: torch.Tensor
) -> torch.futures.Future:
    group_to_use = process_group if process_group is not None else dist.group.WORLD

    "Averages the input gradient tensor by allreduce and returns a future."
    fut = dist.all_reduce(tensor, group=group_to_use, async_op=True).get_future()

    def div_by_group_size(fut):
        return [fut.value()[0].div_(group_to_use.size())]

    return fut.then(div_by_group_size)


def allreduce_hook(
    process_group: dist.ProcessGroup, bucket: dist.GradBucket
) -> torch.futures.Future:
    """
    This DDP communication hook just calls ``allreduce`` using ``GradBucket``
    tensors. Once gradient tensors are aggregated across all workers, its ``then``
    callback takes the mean and returns the result. If user registers this hook,
    DDP results is expected to be same as the case where no hook was registered.
    Hence, this won't change behavior of DDP and user can use this as a reference
    or modify this hook to log useful information or any other purposes while
    unaffecting DDP behavior.

    Example::
        >>> ddp_model.register_comm_hook(process_group, allreduce_hook)
    """
    return _allreduce_fut(process_group, bucket.get_tensors()[0])


def fp16_compress_hook(
    process_group: dist.ProcessGroup, bucket: dist.GradBucket
) -> torch.futures.Future:
    """
    This DDP communication hook implements a simple gradient compression
    approach that casts ``GradBucket`` tensors to half-precision floating-point format (``torch.float16``).
    It allreduces those ``float16`` gradient tensors. Once compressed gradient
    tensors are allreduced, the chained callback ``decompress`` first averages the aggregate result on all the processes,
    and then casts it back to the input data type (such as ``float32``).

    Example::
        >>> ddp_model.register_comm_hook(process_group, fp16_compress_hook)
    """
    group_to_use = process_group if process_group is not None else dist.group.WORLD
    world_size = group_to_use.size()

    compressed_tensor = bucket.get_tensors()[0].to(torch.float16)

    fut = dist.all_reduce(
        compressed_tensor, group=group_to_use, async_op=True
    ).get_future()

    def decompress(fut):
        decompressed_tensor = bucket.get_tensors()[0]
        # Decompress in place to reduce the peak memory.
        # See: https://github.com/pytorch/pytorch/issues/45968
        decompressed_tensor.copy_(fut.value()[0].div_(world_size))
        return [decompressed_tensor]

    return fut.then(decompress)

def fp16_compress_wrapper(
    hook: Callable[[Any, dist.GradBucket], torch.futures.Future]
) -> Callable[[Any, dist.GradBucket], torch.futures.Future]:
    """
    This wrapper casts the input gradient tensors of a given DDP communication hook to half-precision
    floating point format (``torch.float16``), and casts the resulting tensors of the given hook back to
    the input data type, such as ``float32``.
    Example::
    >>> state = PowerSGDState(process_group=process_group, matrix_approximation_rank=1, start_powerSGD_iter=10)
    >>> ddp_model.register_comm_hook(state, fp16_compress_wrapper(powerSGD_hook))
    """

    def fp16_compress_wrapper_hook(hook_state, bucket: dist.GradBucket) -> torch.futures.Future:
        # Overwrite bucket tensors to the fp16 cast tensors.
        bucket.set_tensor(bucket.get_tensors()[0].to(torch.float16), 0)

        fut = hook(hook_state, bucket)

        def decompress(fut):
            decompressed_tensor = bucket.get_tensors()[0]
            # Decompress in place to reduce the peak memory.
            # See: https://github.com/pytorch/pytorch/issues/45968
            decompressed_tensor.copy_(fut.value()[0])
            return [decompressed_tensor]

        # Decompress after hook has run.
        return fut.then(decompress)

    return fp16_compress_wrapper_hook
