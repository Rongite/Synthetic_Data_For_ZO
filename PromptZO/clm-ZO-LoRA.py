#!/usr/bin/env python
# coding=utf-8
# Copyright 2021 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Fine-tuning the library models for causal language modeling (GPT, GPT-2, CTRL, ...)
on a text file or a dataset without using HuggingFace Trainer.

Here is the full list of checkpoints on the hub that can be fine-tuned by this script:
https://huggingface.co/models?filter=text-generation
"""
# You can also adapt this script on your own causal language modeling task. Pointers for this are left as comments.

import argparse
import json
import logging
import math
import os
import random
from itertools import chain
import sys

import datasets
import torch
from accelerate import Accelerator, DistributedType
from accelerate.logging import get_logger
from accelerate.utils import set_seed
from datasets import load_dataset
from huggingface_hub import Repository, create_repo
from torch.utils.data import DataLoader
from tqdm.auto import tqdm
from eventProfiler import EventProfiler
import transformers
from transformers import (
    CONFIG_MAPPING,
    MODEL_MAPPING,
    AutoConfig,
    AutoModelForCausalLM,
    AutoTokenizer,
    SchedulerType,
    default_data_collator,
    get_scheduler,
)
from ZO import *

logger = get_logger(__name__)

MODEL_CONFIG_CLASSES = list(MODEL_MAPPING.keys())
MODEL_TYPES = tuple(conf.model_type for conf in MODEL_CONFIG_CLASSES)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Finetune a transformers model on a causal language modeling task")
    parser.add_argument(
        "--dataset_name",
        type=str,
        default="wikitext",
        help="The name of the dataset to use (via the datasets library).",
    )
    parser.add_argument(
        "--dataset_config_name",
        type=str,
        default='wikitext-2-raw-v1',
        help="The configuration name of the dataset to use (via the datasets library).",
    )
    parser.add_argument(
        "--model_name_or_path",
        type=str,
        help="Path to pretrained model or model identifier from huggingface.co/models.",
        default="facebook/opt-1.3b"
    )
    parser.add_argument(
        "--config_name",
        type=str,
        default=None,
        help="Pretrained config name or path if not the same as model_name",
    )
    parser.add_argument(
        "--tokenizer_name",
        type=str,
        default=None,
        help="Pretrained tokenizer name or path if not the same as model_name",
    )
    parser.add_argument(
        "--per_device_train_batch_size",
        type=int,
        default=32,
        help="Batch size (per device) for the training dataloader.",
    )
    parser.add_argument(
        "--per_device_eval_batch_size",
        type=int,
        default=16,
        help="Batch size (per device) for the evaluation dataloader.",
    )
    parser.add_argument(
        "--learning_rate",
        type=float,
        default=1e-6,
        help="Initial learning rate (after the potential warmup period) to use.",
    )
    parser.add_argument("--num_train_epochs", type=int, default=3,
                        help="Total number of training epochs to perform.")
    parser.add_argument(
        "--max_train_steps",
        type=int,
        default=5000,
        help="Total number of training steps to perform. If provided, overrides num_train_epochs.",
    )
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=1,
        help="Number of updates steps to accumulate before performing a backward/update pass.",
    )
    parser.add_argument(
        "--lr_scheduler_type",
        type=SchedulerType,
        default="linear",
        help="The scheduler type to use.",
        choices=["linear", "cosine", "cosine_with_restarts",
                 "polynomial", "constant", "constant_with_warmup"],
    )
    parser.add_argument(
        "--num_warmup_steps", type=int, default=0, help="Number of steps for the warmup in the lr scheduler."
    )
    parser.add_argument("--seed", type=int, default=0,
                        help="A seed for reproducible training.")
    parser.add_argument(
        "--model_type",
        type=str,
        default=None,
        help="Model type to use if training from scratch.",
        choices=MODEL_TYPES,
    )
    parser.add_argument(
        "--block_size",
        type=int,
        default=1024,
        help=(
            "Optional input sequence length after tokenization. The training dataset will be truncated in block of"
            " this size for training. Default to the model max input length for single sentence inputs (take into"
            " account special tokens)."
        ),
    )
    parser.add_argument(
        "--preprocessing_num_workers",
        type=int,
        default=None,
        help="The number of processes to use for the preprocessing.",
    )
    parser.add_argument(
        "--no_keep_linebreaks", action="store_true", help="Do not keep line breaks when using TXT files."
    )
    parser.add_argument(
        "--adam",
        action="store_true",
    )
    parser.add_argument(
        "--momentum",
        action="store_true"
    )
    parser.add_argument(
        "--SGD",
        action="store_true"
    )
    parser.add_argument(
        "--linear_lr",
        action="store_true"
    )
    parser.add_argument(
        "--cosine_lr",
        action="store_true"
    )
    parser.add_argument(
        "--cosine_lr_with_warmup",
        action="store_true"
    )
    parser.add_argument(
        "--momentum_mu",
        type=float,
        default=0.9,
    )
    parser.add_argument(
        "--adam_beta1",
        type=float,
        default=0.9,
    )
    parser.add_argument(
        "--adam_beta2",
        type=float,
        default=0.999,
    )
    parser.add_argument(
        "--weight_decay",
        type=float,
        default=0
    )
    parser.add_argument(
        "--lora_rank",
        type=int,
        default=-1
    )
    parser.add_argument(
        "--lora_basic_rank",
        type=int,
        default=16
    )
    parser.add_argument(
        "--zo_sample",
        type=int,
        default=4
    )
    parser.add_argument(
        "--ZO_eps",
        type=float,
        default=1e-2
    )
    parser.add_argument(
        "--init_pc_head",
        action='store_true',
    )
    parser.add_argument(
        "--init_pca_proj",
        action='store_true',
    )
    parser.add_argument(
        "--init_pc_sample",
        action='store_true',
    )
    parser.add_argument(
        "--init_pc_tail",
        action='store_true',
    )
    parser.add_argument(
        "--init_pc_random_sample",
        action='store_true',
    )
    parser.add_argument(
        "--init_pc_sample_w_pcs",
        action='store_true',
    )
    parser.add_argument(
        "--init_svd_column_space_random",
        action='store_true',
    )
    parser.add_argument(
        "--init_svd_column_space_top",
        action='store_true',
    )
    parser.add_argument(
        "--init_svd_row_space_random",
        action='store_true',
    )
    parser.add_argument(
        "--random_orthonormal",
        action='store_true',
    )
    parser.add_argument(
        "--svd_twice",
        action='store_true',
    )
    parser.add_argument(
        "--vanilla_two_A",
        action='store_true',
    )
    parser.add_argument(
        "--new_interm",
        action='store_true',
    )
    parser.add_argument(
        "--divided_by_d",
        action='store_true'
    )
    parser.add_argument(
        "--reparam_vanilla",
        action='store_true'
    )
    parser.add_argument(
        "--one_column_update",
        action='store_true'
    )
    args = parser.parse_args()
    return args


@torch.no_grad()
def eval(model, eval_dataloader):
    model.eval()
    losses = []
    for step, batch in enumerate(eval_dataloader):
        batch = {k: v.cuda() for k, v in batch.items()}
        outputs = model(**batch)
        loss = outputs.loss
        losses.append(loss.repeat(len(batch['input_ids'])))

    losses = torch.cat(losses)
    try:
        eval_loss = torch.mean(losses)
        perplexity = math.exp(eval_loss)
    except OverflowError:
        perplexity = float("inf")
    model.train()
    return perplexity


def write_results(args, losses, ppl):
    tag = args.dataset_name + '-' + args.dataset_config_name + '-' + \
        args.model_name_or_path.replace(os.sep, '-') + '-' + \
        str(args.block_size) + "-"
    if args.adam:
        tag += "adam"
    elif args.momentum:
        tag += "momentum"
    elif args.SGD:
        tag += "SGD"
    else:
        raise NotImplementedError()

    if args.init_pca_proj:
        tag += "-init_pca_proj"
    elif args.init_pc_head:
        tag += "-init_pc_head"
    elif args.init_pc_tail:
        tag += "-init_pc_tail"
    elif args.init_pc_random_sample:
        tag += "-init_pc_random_sample"
    elif args.init_pc_sample_w_pcs:
        tag += "-init_pc_sample_w_pcs"
    elif args.init_svd_column_space_random:
        tag += "-init_svd_column_space_random"
    elif args.init_svd_column_space_top:
        tag += "-init_svd_column_space_top"
    elif args.init_svd_row_space_random:
        tag += "-init_svd_row_space_random"
    elif args.random_orthonormal:
        tag += "-random_orthonormal"
    elif args.svd_twice:
        tag += "-svd_twice"
    elif args.vanilla_two_A:
        tag += "-vanilla_two_A"
    elif args.new_interm:
        tag += "-new_interm"
    elif args.reparam_vanilla:
        tag += "-reparam_vanilla"
    elif args.one_column_update:
        tag += "-one_column_update"

    if args.divided_by_d:
        tag += "-divided_by_d"

    if args.lora_rank != -1:
        tag += f"-{args.lora_basic_rank}-{args.lora_rank}-seed-{args.seed}-lr-{args.learning_rate}-eps-{args.ZO_eps}"
    else:
        tag += f"-{args.lora_basic_rank}-seed-{args.seed}-lr-{args.learning_rate}-eps-{args.ZO_eps}"

    torch.save(losses, f'result/lora-ZO/{tag}-loss.pt')
    torch.save(ppl, f'result/lora-ZO/{tag}-ppl.pt')



def main():
    args = parse_args()
    total_batch_size = args.per_device_train_batch_size * \
        args.gradient_accumulation_steps

    # Make one log on every process with the configuration for debugging.
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
    )

    set_seed(args.seed)

    # Get the datasets: you can either provide your own CSV/JSON/TXT training and evaluation files (see below)
    # or just provide the name of one of the public datasets available on the hub at https://huggingface.co/datasets/
    # (the dataset will be downloaded automatically from the datasets Hub).
    #
    # For CSV/JSON files, this script will use the column called 'text' or the first column if no column called
    # 'text' is found. You can easily tweak this behavior (see below).
    #
    # In distributed training, the load_dataset function guarantee that only one local process can concurrently
    # download the dataset.
    # Downloading and loading a dataset from the hub.
    raw_datasets = load_dataset(
        args.dataset_name, args.dataset_config_name)
    if "validation" not in raw_datasets.keys():
        raw_datasets["validation"] = load_dataset(
            args.dataset_name,
            args.dataset_config_name,
            split=f"train[:{args.validation_split_percentage}%]",
        )
        raw_datasets["train"] = load_dataset(
            args.dataset_name,
            args.dataset_config_name,
            split=f"train[{args.validation_split_percentage}%:]",
        )

    # See more about loading any type of standard or custom dataset (from files, python dict, pandas DataFrame, etc) at
    # https://huggingface.co/docs/datasets/loading_datasets.html.

    # Load pretrained model and tokenizer
    #
    # In distributed training, the .from_pretrained methods guarantee that only one local process can concurrently
    # download model & vocab.
    if args.config_name:
        config = AutoConfig.from_pretrained(args.config_name)
    elif args.model_name_or_path:
        config = AutoConfig.from_pretrained(args.model_name_or_path)
    else:
        config = CONFIG_MAPPING[args.model_type]()
        logger.warning(
            "You are instantiating a new config instance from scratch.")
    config.lora_r = args.lora_basic_rank
    config.extra_r = args.lora_rank

    config.use_cache = False
    if args.tokenizer_name:
        tokenizer = AutoTokenizer.from_pretrained(
            args.tokenizer_name, use_fast=True)
    elif args.model_name_or_path:
        tokenizer = AutoTokenizer.from_pretrained(
            args.model_name_or_path, use_fast=True)
    else:
        raise ValueError(
            "You are instantiating a new tokenizer from scratch. This is not supported by this script."
            "You can do it from another script, save it, and load it from here, using --tokenizer_name."
        )

    if args.svd_twice or args.vanilla_two_A:
        config.two_way_lora_A = True
    else:
        config.two_way_lora_A = False

    dtype = torch.float16
    import modeling_opt_lora
    model = modeling_opt_lora.OPTForCausalLM.from_pretrained(
        args.model_name_or_path,
        config=config,
        torch_dtype=dtype,
    ).cuda()
    import sklearn.decomposition, numpy as np
    model_params = {n : p for n, p in model.named_parameters()}
    if args.one_column_update:
        update_and_cache_parameters_per_client = [dict() for _ in range(args.lora_basic_rank)]
    with torch.no_grad():
        for n, p in model.named_parameters():
            if 'lora' not in n:
                p.requires_grad_(False)
            else:
                if args.one_column_update:
                    if 'lora_A' in n:
                        for i in range(args.lora_basic_rank):
                            update_and_cache_parameters_per_client[i][n] = (p[i, :], p.clone())
                    else:
                        for i in range(args.lora_basic_rank):
                            update_and_cache_parameters_per_client[i][n] = (p[:, i], p.clone())

                if args.vanilla_two_A and 'lora_A_1' in n:
                    W = model_params[n.replace('.lora_A_1', '') + '.weight']
                    U_old, _, _ = torch.svd(W.float()) # (2048, 16) (16, 64), (64, 16)
                    U, S, V = torch.linalg.svd(U_old[:, :config.lora_r])
                    model_params[n].data.copy_(
                        (U[:, :config.extra_r] @ torch.diag(S[:config.extra_r]).sqrt()).T)
                    model_params[n.replace('lora_A_1', 'lora_A_2')].data.copy_(
                        (V[:config.extra_r, :].T @ torch.diag(S[:config.extra_r]).sqrt()))
                # if 'lora_AB_interm_1' in n and args.random_orthonormal:
                #     orthovecs = random_orthonormal(p.shape[0], p.shape[1], device=p.device).to(dtype=p.dtype)
                #     p.data.copy_(orthovecs.T)

                # elif 'lora_AB_interm_2' in n and args.random_orthonormal:
                #     orthovecs = random_orthonormal(p.shape[1], p.shape[0], device=p.device).to(dtype=p.dtype)
                #     p.data.copy_(orthovecs)

                # elif 'lora_A' in n and ('lora_AB_interm_1' not in n and 'lora_AB_interm_2' not in n \
                #                         and 'lora_A_1' not in n and not 'lora_A_2' in n):
                #     W = model_params[n.replace('.lora_A', '') + '.weight']
                    
                #     if args.init_pca_proj:
                #         pca = sklearn.decomposition.PCA(n_components=p.shape[0])
                #         W_projected = pca.fit_transform(W.float().cpu().numpy())
                #         W_projected = torch.from_numpy(W_projected).cuda()
                #         p.data.copy_(W_projected.T)
                    
                #     elif args.init_pc_head:
                #         pca = sklearn.decomposition.PCA(n_components=p.shape[0])
                #         pca.fit(W.float().cpu().numpy())
                #         W_pcs = pca.components_
                #         W_pcs = torch.from_numpy(W_pcs).cuda()
                #         p.data.copy_(W_pcs)
                    
                #     elif args.init_pc_tail:
                #         pca = sklearn.decomposition.PCA()
                #         pca.fit(W.float().cpu().numpy())
                #         W_pcs = pca.components_
                #         W_pcs = torch.from_numpy(W_pcs).cuda()
                #         p.data.copy_(W_pcs[-p.shape[0]:])
                    
                #     elif args.init_pc_random_sample:
                #         pca = sklearn.decomposition.PCA()
                #         pca.fit(W.float().cpu().numpy())
                #         W_pcs = pca.components_
                #         W_pcs = torch.from_numpy(W_pcs).cuda()
                #         indices = torch.from_numpy(np.random.permutation(np.arange(2048))[:p.shape[0]])
                #         p.data.copy_(W_pcs[indices])
                    
                #     elif args.init_pc_sample_w_pcs:
                #         pca = sklearn.decomposition.PCA()
                #         pca.fit(W.float().cpu().numpy())
                #         W_pcs = pca.components_
                #         W_pcs = torch.from_numpy(W_pcs).cuda()
                #         probs = pca.explained_variance_ratio_
                #         indices = torch.from_numpy(np.random.choice(np.arange(2048), size=p.shape[0], p=probs))
                #         p.data.copy_(W_pcs[indices])
                    
                #     elif args.random_orthonormal:
                #         orthovecs = random_orthonormal(p.shape[1], p.shape[0], device=p.device).to(dtype=p.dtype)
                #         p.data.copy_(orthovecs)
                    
                #     elif args.init_svd_column_space_top:
                #         U, S, VT = torch.svd(W.float())
                #         p.data.copy_(U[:p.shape[0]])
                #         del U, S, VT
                    
                #     elif args.init_svd_column_space_random:
                #         W = model_params[n.replace('.lora_A', '') + '.weight']
                #         U, S, VT = torch.svd(W.float())
                #         indices = torch.from_numpy(np.random.permutation(np.arange(2048))[:p.shape[0]])
                #         p.data.copy_(U[indices])
                #         del U, S, VT
                    
                #     elif args.init_svd_row_space_random:
                #         W = model_params[n.replace('.lora_A', '') + '.weight']
                #         U, S, VT = torch.svd(W.float())
                #         indices = torch.from_numpy(np.random.permutation(np.arange(2048))[:p.shape[0]])
                #         p.data.copy_(VT[:, indices].T)
                #         del U, S, VT

                if 'lora_AB_interm_1' in n and args.new_interm:
                    W = model_params[n.replace('.lora_AB_interm_1', '') + '.weight']
                    U_old, _, _ = torch.svd(W.float()) # (2048, 16) (16, 64), (64, 16)
                    U, S, V = torch.svd(U_old[:, :config.lora_r])
                    # 2048 4, 4 16, 16 4, 4 2048
                    model_params[n.replace('lora_AB_interm_1', 'lora_A')].data.copy_(U.T)
                    model_params[n].data.copy_(
                        torch.hstack([torch.diag(S), torch.zeros(config.lora_r, config.extra_r - config.lora_r, device=S.device, dtype=S.dtype)]).T
                    )
                    # if args.divided_by_d:
                    #     model_params[n].data.div_(math.sqrt(config.extra_r - config.lora_r))
                    model_params[n.replace('lora_AB_interm_1', 'lora_AB_interm_2')].data.copy_(
                        torch.hstack([V, torch.randn(config.lora_r, config.extra_r - config.lora_r, device=V.device, dtype=V.dtype)])
                    )
                    if args.divided_by_d:
                        model_params[n.replace('lora_AB_interm_1', 'lora_AB_interm_2')] /= \
                            model_params[n.replace('lora_AB_interm_1', 'lora_AB_interm_2')].norm(dim=0).unsqueeze(0)

                # elif 'lora_A_1' in n and not args.vanilla_two_A:
                #     W = model_params[n.replace('.lora_A_1', '') + '.weight']
                #     U_old, _, _ = torch.svd(W.float())
                #     U, V = LoRA_more_rank_2_way_LoRA(U_old[:, :config.lora_r].float(), config.extra_r)
                #     model_params[n].data.copy_(U.T)
                #     model_params[n.replace('.lora_A_1', '.lora_A_2')].data.copy_(V[:, :config.lora_r].T)

    if args.reparam_vanilla:
        args.lora_rank = -1
        args.divided_by_d = None
        args.new_interm = None
        config.extra_r = -1
        another_model = modeling_opt_lora.OPTForCausalLM.from_pretrained(
            args.model_name_or_path,
            config=config,
            torch_dtype=dtype,
        )
        with torch.no_grad():
            for n, p in another_model.named_parameters():
                if 'lora' not in n:
                    p.requires_grad_(False)
                elif 'lora_A' in n:
                    A = model_params[n].T @ \
                        model_params[n.replace('.lora_A', '.lora_AB_interm_1')].T @ \
                        model_params[n.replace(
                            '.lora_A', '.lora_AB_interm_2')].T
                    p.data.copy_(A.T)
            model = another_model
            del model_params  
            model = model.cuda()
            torch.cuda.empty_cache()

    embedding_size = model.get_input_embeddings().weight.shape[0]
    if len(tokenizer) > embedding_size:
        model.resize_token_embeddings(len(tokenizer))    

    # Preprocessing the datasets.
    # First we tokenize all the texts.
    column_names = raw_datasets["train"].column_names
    text_column_name = "text" if "text" in column_names else column_names[0]

    def tokenize_function(examples):
        return tokenizer(examples[text_column_name])

    tokenized_datasets = raw_datasets.map(
        tokenize_function,
        batched=True,
        num_proc=args.preprocessing_num_workers,
        remove_columns=column_names,
        desc="Running tokenizer on dataset",
    )

    if args.block_size is None:
        block_size = tokenizer.model_max_length
    else:
        if args.block_size > tokenizer.model_max_length:
            logger.warning(
                f"The block_size passed ({args.block_size}) is larger than the maximum length for the model"
                f"({tokenizer.model_max_length}). Using block_size={tokenizer.model_max_length}."
            )
        block_size = min(args.block_size, tokenizer.model_max_length)

    # Main data processing function that will concatenate all texts from our dataset and generate chunks of block_size.
    def group_texts(examples):
        # Concatenate all texts.
        concatenated_examples = {
            k: list(chain(*examples[k])) for k in examples.keys()}
        total_length = len(concatenated_examples[list(examples.keys())[0]])
        # We drop the small remainder, and if the total_length < block_size  we exclude this batch and return an empty dict.
        # We could add padding if the model supported it instead of this drop, you can customize this part to your needs.
        total_length = (total_length // block_size) * block_size
        # Split by chunks of max_len.
        result = {
            k: [t[i: i + block_size]
                for i in range(0, total_length, block_size)]
            for k, t in concatenated_examples.items()
        }
        result["labels"] = result["input_ids"].copy()
        return result

    # Note that with `batched=True`, this map processes 1,000 texts together, so group_texts throws away a remainder
    # for each of those groups of 1,000 texts. You can adjust that batch_size here but a higher value might be slower
    # to preprocess.
    #
    # To speed up this part, we use multiprocessing. See the documentation of the map method for more information:
    # https://huggingface.co/docs/datasets/package_reference/main_classes.html#datasets.Dataset.map

    lm_datasets = tokenized_datasets.map(
        group_texts,
        batched=True,
        num_proc=args.preprocessing_num_workers,
        desc=f"Grouping texts in chunks of {block_size}",
    )

    train_dataset = lm_datasets["train"]
    eval_dataset = lm_datasets["validation"]

    # DataLoaders creation:
    train_dataloader = DataLoader(
        train_dataset, shuffle=True, collate_fn=default_data_collator, batch_size=args.per_device_train_batch_size)
    eval_dataloader = DataLoader(
        eval_dataset, collate_fn=default_data_collator, batch_size=args.per_device_eval_batch_size
    )
    
    ppl = eval(model, eval_dataloader)
    print(f'ppl {ppl:.3f}')

    # if args.one_column_update:
    #     if args.momentum:
    #         optimizers = [
    #             torch.optim.SGD(pair[0], lr=args.learning_rate, momentum=args.momentum_mu, weight_decay=args.weight_decay) \
    #                 for pair in update_and_cache_parameters_per_client
    #         ]
    #     else:
    #         raise NotImplementedError()
    # else:
    #     if args.adam:
    #         optimizer = torch.optim.Adam(model.parameters(), lr=args.learning_rate, \
    #                                     betas=(args.adam_beta1, args.adam_beta2), weight_decay=args.weight_decay)
    #     elif args.momentum:
    #         optimizer = torch.optim.SGD(model.parameters(), lr=args.learning_rate, 
    #                         momentum=args.momentum_mu, weight_decay=args.weight_decay)
    #     elif args.SGD:
    #         optimizer = torch.optim.SGD(
    #             model.parameters(), lr=args.learning_rate, weight_decay=args.weight_decay)
    #     else:
    #         raise NotImplementedError()
    #     print(optimizer)

    if args.adam:
        optimizer = torch.optim.Adam(model.parameters(), lr=args.learning_rate, \
                                    betas=(args.adam_beta1, args.adam_beta2), weight_decay=args.weight_decay)
    elif args.momentum:
        optimizer = torch.optim.SGD(model.parameters(), lr=args.learning_rate, 
                        momentum=args.momentum_mu, weight_decay=args.weight_decay)
    elif args.SGD:
        optimizer = torch.optim.SGD(
            model.parameters(), lr=args.learning_rate, weight_decay=args.weight_decay)
    else:
        raise NotImplementedError()
    print(optimizer)

    # Scheduler and math around the number of training steps.
    overrode_max_train_steps = False
    num_update_steps_per_epoch = math.ceil(
        len(train_dataloader) / args.gradient_accumulation_steps)
    if args.max_train_steps is None:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
        overrode_max_train_steps = True

    if args.linear_lr:
        lr_scheduler = transformers.get_linear_schedule_with_warmup(
            optimizer, int(0.01 * args.max_train_steps), args.max_train_steps)

    elif args.cosine_lr:
        lr_scheduler = transformers.get_cosine_schedule_with_warmup(
            optimizer, 0, args.max_train_steps)

    elif args.cosine_lr_with_warmup:
        lr_scheduler = transformers.get_cosine_schedule_with_warmup(
            optimizer, int(0.1 * args.max_train_steps), args.max_train_steps)

    else:
        lr_scheduler = transformers.get_constant_schedule(optimizer)

    # We need to recalculate our total training steps as the size of the training dataloader may have changed.
    num_update_steps_per_epoch = math.ceil(
        len(train_dataloader) / args.gradient_accumulation_steps)
    if overrode_max_train_steps:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
    # Afterwards we recalculate our number of training epochs
    args.num_train_epochs = math.ceil(
        args.max_train_steps / num_update_steps_per_epoch)

    # Train!

    print("***** Running training *****")
    print(f"  Num examples = {len(train_dataset)}")
    print(f"  Num Epochs = {args.num_train_epochs}")
    print(
        f"  Instantaneous batch size per device = {args.per_device_train_batch_size}")
    print(
        f"  Total train batch size (w. parallel, distributed & accumulation) = {total_batch_size}")
    print(f"  Total optimization steps = {args.max_train_steps}")
    # Only show the progress bar once on each machine.
    progress_bar = tqdm(range(args.max_train_steps))
    # profiler = EventProfiler(torch.device('cuda'))
    completed_steps = 0
    logging_interval = 10
    eval_steps = 50
    cur_loss = 0
    losses = []
    ppls = []

    with torch.no_grad():
        while completed_steps < args.max_train_steps:
            for step, batch in enumerate(train_dataloader):
                model.eval()
                batch = {k: v.cuda() for k, v in batch.items()}
                if args.one_column_update:
                    zo_sample = args.zo_sample
                    for i in range(args.lora_basic_rank):
                        # perturb_parameters_one_column_LoRA(model, 
                        #                                    i, 
                        #                                    update_and_cache_parameters_per_client[i],
                        #                                    need_perturb=False,
                        #                                    )

                        for _ in range(zo_sample):
                            random_vectors = None
                            model, random_vectors =  \
                                perturb_parameters_one_column_LoRA(
                                    model, 
                                    i, 
                                    update_and_cache_parameters_per_client[i],
                                    need_perturb=True,
                                    random_vectors=random_vectors,
                                    eps=args.ZO_eps,
                                    scaling_factor=1
                                )
                            loss1 = model(**batch).loss

                            model, random_vectors = \
                                perturb_parameters_one_column_LoRA(
                                    model,
                                    i,
                                    update_and_cache_parameters_per_client[i],
                                    need_perturb=True,
                                    random_vectors=random_vectors,
                                    eps=args.ZO_eps,
                                    scaling_factor=-2
                                )
                            loss2 = model(**batch).loss

                            model, random_vectors = \
                                perturb_parameters_one_column_LoRA(
                                    model,
                                    i,
                                    update_and_cache_parameters_per_client[i],
                                    need_perturb=True,
                                    random_vectors=random_vectors,
                                    eps=args.ZO_eps,
                                    scaling_factor=1
                                )
                            cur_loss += (loss1 + loss2) / (2 * zo_sample * args.lora_basic_rank)
                            projected_grad = (loss1 - loss2) / (args.ZO_eps)

                            for n, p in model.named_parameters():
                                if not p.requires_grad:
                                    continue
                                else:
                                    g = projected_grad * random_vectors[n]
                                    if p.grad is None:
                                        p.grad = torch.zeros_like(p)
                                    if 'lora_A' in n:    
                                        p.grad[i, :].add_(g, alpha=args.zo_sample)
                                    else:
                                        p.grad[:, i].add_(g, alpha=args.zo_sample)

                        # optimizers[i].step()
                        # optimizers[i].zero_grad(set_to_none=True)
                    optimizer.step()
                    optimizer.zero_grad(set_to_none=True)

                    for n, p in model.named_parameters():
                        if 'lora' not in n:
                            continue
                        else:
                            if 'lora_A' in n:
                                for i in range(args.lora_basic_rank):
                                    update_and_cache_parameters_per_client[i][n] = (p[i, :], p)
                                    # update_and_cache_parameters_per_client[i][n] = (p[i, :], p.clone())
                            else:
                                for i in range(args.lora_basic_rank):
                                    update_and_cache_parameters_per_client[i][n] = (p[:, i], p)
                                    # update_and_cache_parameters_per_client[i][n] = (p[:, i], p.clone())
                    progress_bar.update(1)
                    completed_steps += 1
                else:
                    zo_sample = args.zo_sample
                    for _ in range(zo_sample):
                        random_vectors = None
                        model, random_vectors = perturb_parameters(model, args.ZO_eps, random_vectors, scaling_factor=1)                    
                        loss1 = model(**batch).loss

                        model, random_vectors = perturb_parameters(model, args.ZO_eps, random_vectors, scaling_factor=-2)
                        loss2 = model(**batch).loss

                        model, random_vectors = perturb_parameters(model, args.ZO_eps, random_vectors, scaling_factor=1)
                        cur_loss += (loss1 + loss2) / (2 * zo_sample)
                        projected_grad = (loss1 - loss2) / (args.ZO_eps)

                        for n, p in model.named_parameters():
                            if not p.requires_grad: continue
                            g = projected_grad * random_vectors[n]
                            if p.grad is None:
                                p.grad = g.div_(args.zo_sample)
                            else:
                                p.grad.add_(g, alpha=1/args.zo_sample)

                    optimizer.step()
                    optimizer.zero_grad(set_to_none=True)
                    lr_scheduler.step()
                    progress_bar.update(1)
                    completed_steps += 1
       
                if completed_steps > 0 and completed_steps % logging_interval == 0:
                    cur_loss /= logging_interval
                    print(f'loss {cur_loss.item()}', flush=True)
                    sys.stdout.flush()
                    losses.append(cur_loss.item())
                    cur_loss.zero_()

                if completed_steps > 0 and completed_steps % eval_steps == 0:
                    ppl = eval(model, eval_dataloader)
                    ppls.append(ppl)
                    print(f"ppl: {ppl}", flush=True)
                    sys.stdout.flush()
                    write_results(args, losses, ppls)

                # if completed_steps > 0 and completed_steps % 1000 == 0:
                #     if args.adam:
                #         optimizer_name = 'adam'
                #     elif args.momentum:
                #         optimizer_name = 'momentum'
                #     elif args.SGD:
                #         optimizer_name = 'SGD'
                #     tag = args.model_name_or_path.replace(os.sep, '-')
                #     if args.init_pca_proj:
                #         tag += "-init_pca_proj"
                #     elif args.init_pc_head:
                #         tag += "-init_pc_head"
                #     elif args.init_pc_tail:
                #         tag += "-init_pc_tail"
                #     elif args.init_pc_random_sample:
                #         tag += "-init_pc_random_sample"
                #     elif args.init_pc_sample_w_pcs:
                #         tag += "-init_pc_sample_w_pcs"
                #     elif args.init_svd_column_space_random:
                #         tag += "-init_svd_column_space_random"
                #     elif args.init_svd_column_space_top:
                #         tag += "-init_svd_column_space_top"
                #     elif args.init_svd_row_space_random:
                #         tag += "-init_svd_row_space_random"
                #     elif args.random_orthonormal:
                #         tag += "-random_orthonormal"
                #     elif args.svd_twice:
                #         tag += "-svd_twice"
                #     elif args.vanilla_two_A:
                #         tag += "-vanilla_two_A"
                #     elif args.new_interm:
                #         tag += "-new_interm"
                #     elif args.reparam_vanilla:
                #         tag += "-reparam_vanilla"

                #     if args.divided_by_d:
                #         tag += "-divided_by_d"

                #     if args.lora_rank != -1:
                #         torch.save({n : p for n, p in model.named_parameters() if p.requires_grad},
                #                 f'model/lora-ZO/{tag}-{optimizer_name}-{args.dataset_name}-{args.dataset_config_name}-step{completed_steps}_model_{args.block_size}-{args.lora_basic_rank}-{args.lora_rank}-{args.learning_rate}-{args.ZO_eps}-{args.seed}.pt')
                #     else:
                #         torch.save({n: p for n, p in model.named_parameters() if p.requires_grad},
                #                    f'model/lora-ZO/{tag}-{optimizer_name}-{args.dataset_name}-{args.dataset_config_name}-step{completed_steps}_model_{args.block_size}-{args.lora_basic_rank}-{args.learning_rate}-{args.ZO_eps}-{args.seed}.pt')

                if completed_steps >= args.max_train_steps or ppl >= 100:
                    break



if __name__ == "__main__":
    main()
