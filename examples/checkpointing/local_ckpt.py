import argparse
import logging
import os
import shutil
from typing import Union

import torch
import torch.distributed as dist
import torch.nn as nn

from nvidia_resiliency_ext.checkpointing.async_ckpt.core import AsyncCallsQueue
from nvidia_resiliency_ext.checkpointing.local.basic_state_dict import BasicTensorAwareStateDict
from nvidia_resiliency_ext.checkpointing.local.ckpt_managers.local_manager import \
    LocalCheckpointManager
from nvidia_resiliency_ext.checkpointing.local.replication.group_utils import parse_group_sequence, GroupWrapper
from nvidia_resiliency_ext.checkpointing.local.replication.strategies import CliqueReplicationStrategy

# Set up basic logging configuration
logging.basicConfig(level=logging.INFO)

def parse_args():
    parser = argparse.ArgumentParser(
        description='Local Checkpointing Basic Example',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        '--ckpt_dir',
        default="/tmp/test_local_checkpointing/",
        help="Checkpoint directory for local checkpoints",
    )
    parser.add_argument(
        '--async_save',
        action='store_true',
        help="Enable asynchronous saving of checkpoints.",
    )
    parser.add_argument(
        '--replication',
        action='store_true',
        help="If set, replication of local checkpoints is enabled"
        "Needs to be enabled on all ranks."
    )
    parser.add_argument(
        '--replication_jump',
        default=4,
        type=int,
        help="Specifies `k` such that replica of rank `n` is stored on ranks"
        "`n+k`, `n+2k`, ..., `n+rk`. `r` is the --replication-factor"
        "Needs to be specified if using --replication and have the same value on all ranks",
    )
    parser.add_argument(
        '--replication_factor',
        default=2,
        type=int,
        help="Number of machines storing the replica of a given rank's data",
    )
    return parser.parse_args()


# Define a simple model
class SimpleModel(nn.Module):
    def __init__(self):
        super(SimpleModel, self).__init__()
        self.fc1 = nn.Linear(10, 5)  # Linear layer: input size 10, output size 5
        self.fc2 = nn.Linear(5, 2)  # Linear layer: input size 5, output size 2
        self.activation = nn.ReLU()  # Activation function: ReLU

    def forward(self, x):
        x = self.activation(self.fc1(x))
        x = self.fc2(x)
        return x


def init_distributed_backend(backend="nccl"):
    """
    Initialize the distributed process group for NCCL backend.
    Assumes the environment variables (CUDA_VISIBLE_DEVICES, etc.) are already set.
    """
    try:
        dist.init_process_group(
            backend=backend,  # Use NCCL backend
            init_method="env://",  # Use environment variables for initialization
        )
        logging.info(f"Rank {dist.get_rank()} initialized with {backend} backend.")

        # Ensure each process uses a different GPU
        torch.cuda.set_device(dist.get_rank())
    except Exception as e:
        logging.error(f"Error initializing the distributed backend: {e}")
        raise

def create_checkpoint_manager(args):
    if args.replication:
        logging.info("Creating CliqueReplicationStrategy.")
        repl_process_groups_ranks : List[List[int]] = parse_group_sequence(
            replication_jump=args.replication_jump,
            replication_factor=args.replication_factor,
            world_size=dist.get_world_size()
        )
        repl_process_groups: List[torch.distributed.ProcessGroup] = [
            torch.distributed.new_group(g) for g in repl_process_groups_ranks
        ]
        my_process_group = GroupWrapper.from_list_of_groups(repl_process_groups)
        repl_strategy = CliqueReplicationStrategy(my_process_group)
    else:
        repl_strategy = None

    return LocalCheckpointManager(args.ckpt_dir, repl_strategy=repl_strategy)

def save(args, ckpt_manager, async_queue, model, iteration):
    # Create Tensor-Aware State Dict
    ta_state_dict = BasicTensorAwareStateDict(model.state_dict())

    if args.async_save:
        logging.info("Creating save request.")
        save_request = ckpt_manager.save(ta_state_dict, iteration, is_async=True)

        logging.info("Saving TASD checkpoint...")
        async_queue.schedule_async_request(save_request)

    else:
        logging.info("Saving TASD checkpoint...")
        ckpt_manager.save(ta_state_dict, iteration)


def load(args, ckpt_manager):
    logging.info("Loading TASD checkpoint...")
    iteration = ckpt_manager.find_latest()
    assert iteration != -1, "Local checkpoint has not been found"
    logging.info(f"Found checkpoint from iteration: {iteration}")

    ta_state_dict, ckpt_part_id = ckpt_manager.load()
    logging.info(f"Successfully loaded checkpoint part (id: {ckpt_part_id})")
    return ta_state_dict.state_dict

def main():
    args = parse_args()
    logging.info(f'{args}')

    # Initialize the distributed backend
    init_distributed_backend(backend="nccl")

    # Instantiate the model and move to CUDA
    model = SimpleModel().to("cuda")

    # Instantiate checkpointing classess needed for local checkpointing
    ckpt_manager = create_checkpoint_manager(args)
    async_queue = AsyncCallsQueue() if args.async_save else None

    iteration = 123 # training iteration (used as training state id)

    # Local checkpointing save
    save(args, ckpt_manager, async_queue, model, iteration)

    if args.async_save:
        # Other operations can happen here

        logging.info("Finalize TASD checkpoint saving.")
        async_queue.maybe_finalize_async_calls(blocking=True, no_dist=False)

    # Synchronize processes to ensure all have completed the saving
    dist.barrier()

    # Local checkpointing load
    load(args, ckpt_manager)

    # Synchronize processes to ensure all have completed the loading
    dist.barrier()

    # Clean up checkpoint directory only on rank 0
    if dist.get_rank() == 0:
        logging.info(f"Cleaning up checkpoint directory: {args.ckpt_dir}")
        shutil.rmtree(args.ckpt_dir)


if __name__ == "__main__":
    main()
