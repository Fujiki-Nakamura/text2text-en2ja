#!/usr/bin/env python
# coding: utf-8

# ## Setup - Hide

# In[1]:


'''
import os
from tensorflow.python.profiler import profiler_client

tpu_profile_service_address = os.environ['COLAB_TPU_ADDR'].replace('8470', '8466')
print(profiler_client.monitor(tpu_profile_service_address, 100, 2))
'''


# In[2]:


'''
%tensorflow_version 2.x
import tensorflow as tf
print("Tensorflow version " + tf.__version__)

try:
  tpu = tf.distribute.cluster_resolver.TPUClusterResolver()  # TPU detection
  print('Running on TPU ', tpu.cluster_spec().as_dict()['worker'])
except ValueError:
  raise BaseException('ERROR: Not connected to a TPU runtime; please see the previous cell in this notebook for instructions!')

tf.config.experimental_connect_to_cluster(tpu)
tf.tpu.experimental.initialize_tpu_system(tpu)
tpu_strategy = tf.distribute.experimental.TPUStrategy(tpu)
'''


# In[3]:


'''
%env USE_TORCH=False
os.environ["USE_TORCH"] = "False"
'''


# ## installation, setup

# In[4]:


'''
%cd /content/drive/MyDrive/workspace/text2text-en2ja/
!pwd
'''


# In[5]:


'''
# !pip install -q datasets transformers
!pip install -q flax jax
# get the latest JAX and jaxlib
!pip install --upgrade -q jax jaxlib
'''


# In[6]:


'''
# Colab runtime set to TPU accel
import requests
import os
if 'TPU_DRIVER_MODE' not in globals():
  url = 'http://' + os.environ['COLAB_TPU_ADDR'].split(':')[0] + ':8475/requestversion/tpu_driver_nightly'
  resp = requests.post(url)
  TPU_DRIVER_MODE = 1

# TPU driver as backend for JAX
from jax.config import config
config.FLAGS.jax_xla_backend = "tpu_driver"
config.FLAGS.jax_backend_target = "grpc://" + os.environ['COLAB_TPU_ADDR']
print(config.FLAGS.jax_backend_target)
'''


# In[7]:


'''
!pip install --upgrade pip
!pip install --upgrade "jax[cuda]" -f https://storage.googleapis.com/jax-releases/jax_releases.html
'''


# In[8]:


# !pip install tensorflow


# ## check devices

# In[9]:


import os; os.environ["CUDA_VISIBLE_DEVICES"] = "0,1"


# In[10]:


from jax.lib import xla_bridge
print(xla_bridge.get_backend().platform)


# In[11]:


import jax
jax.devices()
# python -c "import jax; jax.default_backend()"


# ## imports

# In[12]:


import os

os.environ["HF_DATASETS_CACHE"] = os.getenv("HF_DATASETS_CACHE")
os.environ["HF_TOKEN"] = os.getenv("HF_TOKEN")


# In[13]:


import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field

from enum import Enum
from itertools import chain
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
from datasets import load_dataset, Dataset, DatasetDict
from tqdm import tqdm

import flax
import jax
import jax.numpy as jnp
import optax
from flax import jax_utils, traverse_util
from flax.training import train_state
from flax.training.common_utils import get_metrics, onehot, shard
from huggingface_hub import Repository
from transformers import (
    CONFIG_MAPPING, FLAX_MODEL_FOR_MASKED_LM_MAPPING, 
    AutoTokenizer, BatchEncoding, FlaxT5ForConditionalGeneration, HfArgumentParser, PreTrainedTokenizerBase, T5Config,
    is_tensorboard_available, set_seed,
)
from transformers.models.t5.modeling_flax_t5 import shift_tokens_right
from transformers.utils import get_full_repo_name


MODEL_CONFIG_CLASSES = list(FLAX_MODEL_FOR_MASKED_LM_MAPPING.keys())
MODEL_TYPES = tuple(conf.model_type for conf in MODEL_CONFIG_CLASSES)


# ## Arguments

# In[14]:


@dataclass
class ModelArguments:
    model_name_or_path: Optional[str] = field(default="fnakamura/t5-base-en2ja")
    model_type: Optional[str] = field(default="t5")
    config_name: Optional[str] = field(default="fnakamura/t5-base-en2ja")
    tokenizer_name: Optional[str] = field(default="fnakamura/t5-base-en2ja")
    cache_dir: Optional[str] = field(default=os.getenv("HF_DATASETS_CACHE"))
    use_fast_tokenizer: bool = field(default=True)
    dtype: Optional[str] = field(default="float32")
    use_auth_token: bool = field(default=False)

@dataclass
class DataArguments:
    dataset_name: Optional[str] = field(default=None)
    dataset_config_name: Optional[str] = field(default=None)
    # dataset_name: Optional[str] = field(default="oscar")
    # dataset_config_name: Optional[str] = field(default="unshuffled_deduplicated_ja")
    # data_files: Optional[str] = field(default="/content/drive/MyDrive/workspace/text2text-en2ja/datasets/ja_wikipedia/*/*.jsonl")
    data_files: Optional[str] = field(default="/workspace/data/fnakamura/workspace/lm/ja_wikipedia/AA/*.jsonl")
    train_file: Optional[str] = field(default=None)
    validation_file: Optional[str] = field(default=None)
    train_ref_file: Optional[str] = field(default=None)
    validation_ref_file: Optional[str] = field(default=None)
    overwrite_cache: bool = field(default=False)
    validation_split_percentage: Optional[int] = field(default=5)
    max_seq_length: Optional[int] = field(default=128)
    preprocessing_num_workers: Optional[int] = field(default=None)
    mlm_probability: float = field(default=0.15)
    mean_noise_span_length: float = field(default=3.0)

    def __post_init__(self):
        if self.dataset_name is None and self.train_file is None and self.validation_file is None and self.data_files is None:
            raise ValueError("Need either a dataset name or a training/validation file.")
        else:
            if self.train_file is not None:
                extension = self.train_file.split(".")[-1]
                assert extension in ["csv", "json", "txt"], "`train_file` should be a csv, a json or a txt file."
            if self.validation_file is not None:
                extension = self.validation_file.split(".")[-1]
                assert extension in ["csv", "json", "txt"], "`validation_file` should be a csv, a json or a txt file."

@dataclass
class TrainingArguments:
    output_dir: str = field(default="experimental_results/t5-base-en2ja_0")
    overwrite_output_dir: bool = field(default=True)

    do_train: bool = field(default=True)
    do_eval: bool = field(default=False)

    per_device_train_batch_size: int = field(default=4)
    per_device_eval_batch_size: int = field(default=4)
    learning_rate: float = field(default=5e-5)
    weight_decay: float = field(default=0.0)
    adam_beta1: float = field(default=0.9)
    adam_beta2: float = field(default=0.999)
    adam_epsilon: float = field(default=1e-8)
    adafactor: bool = field(default=False)
    num_train_epochs: float = field(default=3.0)
    warmup_steps: int = field(default=0)
    logging_steps: int = field(default=100)
    save_steps: int = field(default=100)
    eval_steps: int = field(default=100)
    push_to_hub: bool = field(default=False)
    # push_to_hub: bool = field(default=True)
    hub_model_id: str = field(default=None)
    hub_token: str = field(default=os.getenv("HF_TOKEN"))
    seed: int = field(default=42)

    def __post_init__(self):
        if self.output_dir is not None:
            self.output_dir = os.path.expanduser(self.output_dir)

    def to_dict(self):
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, Enum): d[k] = v.value  # noqa
            if isinstance(v, list) and len(v) > 0 and isinstance(v[0], Enum): d[k] = [x.value for x in v]
            if k.endswith("_token"): d[k] = f"<{k.uppser()}>"
        return d


# ## Utils

# In[15]:


def compute_input_and_target_lengths(inputs_length, noise_density, mean_noise_span_length):
    """This function is copy of `random_spans_helper <https://github.com/google-research/text-to-text-transfer-transformer/blob/84f8bcc14b5f2c03de51bd3587609ba8f6bbd1cd/t5/data/preprocessors.py#L2466>`__ .
    Training parameters to avoid padding with random_spans_noise_mask.
    When training a model with random_spans_noise_mask, we would like to set the other
    training hyperparmeters in a way that avoids padding.
    This function helps us compute these hyperparameters.
    We assume that each noise span in the input is replaced by extra_tokens_per_span_inputs sentinel tokens,
    and each non-noise span in the targets is replaced by extra_tokens_per_span_targets sentinel tokens.
    This function tells us the required number of tokens in the raw example (for split_tokens())
    as well as the length of the encoded targets. Note that this function assumes
    the inputs and targets will have EOS appended and includes that in the reported length.
    Args:
        inputs_length: an integer - desired length of the tokenized inputs sequence
        noise_density: a float
        mean_noise_span_length: a float
    Returns:
        tokens_length: length of original text in tokens
        targets_length: an integer - length in tokens of encoded targets sequence
    """

    def _tokens_length_to_inputs_length_targets_length(tokens_length):
        num_noise_tokens = int(round(tokens_length * noise_density))
        num_nonnoise_tokens = tokens_length - num_noise_tokens
        num_noise_spans = int(round(num_noise_tokens / mean_noise_span_length))
        # inputs contain all nonnoise tokens, sentinels for all noise spans
        # and one EOS token.
        _input_length = num_nonnoise_tokens + num_noise_spans + 1
        _output_length = num_noise_tokens + num_noise_spans + 1
        return _input_length, _output_length

    tokens_length = inputs_length

    while _tokens_length_to_inputs_length_targets_length(tokens_length + 1)[0] <= inputs_length:
        tokens_length += 1

    inputs_length, targets_length = _tokens_length_to_inputs_length_targets_length(tokens_length)

    # minor hack to get the targets length to be equal to inputs length
    # which is more likely to have been set to a nice round number.
    if noise_density == 0.5 and targets_length > inputs_length:
        tokens_length -= 1
        targets_length -= 1
    return tokens_length, targets_length


@flax.struct.dataclass
class FlaxDataCollatorForT5MLM:
    """
    Data collator used for T5 span-masked language modeling.
    It is made sure that after masking the inputs are of length `data_args.max_seq_length` and targets are also of fixed length.
    For more information on how T5 span-masked language modeling works, one can take a look
    at the `official paper <https://arxiv.org/pdf/1910.10683.pdf>`__
    or the `official code for preprocessing <https://github.com/google-research/text-to-text-transfer-transformer/blob/master/t5/data/preprocessors.py>`__ .
    Args:
        tokenizer (:class:`~transformers.PreTrainedTokenizer` or :class:`~transformers.PreTrainedTokenizerFast`):
            The tokenizer used for encoding the data.
        noise_density (:obj:`float`):
            The probability with which to (randomly) mask tokens in the input.
        mean_noise_span_length (:obj:`float`):
            The average span length of the masked tokens.
        input_length (:obj:`int`):
            The expected input length after masking.
        target_length (:obj:`int`):
            The expected target length after masking.
        pad_token_id: (:obj:`int`):
            The pad token id of the model
        decoder_start_token_id: (:obj:`int):
            The decoder start token id of the model
    """

    tokenizer: PreTrainedTokenizerBase
    noise_density: float
    mean_noise_span_length: float
    input_length: int
    target_length: int
    pad_token_id: int
    decoder_start_token_id: int

    def __call__(self, examples: List[Dict[str, np.ndarray]]) -> Dict[str, np.ndarray]:

        # convert list to dict and tensorize input
        batch = BatchEncoding(
            {k: np.array([examples[i][k] for i in range(len(examples))]) for k, v in examples[0].items()}
        )

        input_ids = batch["input_ids"]
        batch_size, expandend_input_length = input_ids.shape

        mask_indices = np.asarray([self.random_spans_noise_mask(expandend_input_length) for i in range(batch_size)])
        labels_mask = ~mask_indices

        input_ids_sentinel = self.create_sentinel_ids(mask_indices.astype(np.int8))
        labels_sentinel = self.create_sentinel_ids(labels_mask.astype(np.int8))

        batch["input_ids"] = self.filter_input_ids(input_ids, input_ids_sentinel)
        batch["labels"] = self.filter_input_ids(input_ids, labels_sentinel)

        if batch["input_ids"].shape[-1] != self.input_length:
            raise ValueError(
                f"`input_ids` are incorrectly preprocessed. `input_ids` length is {batch['input_ids'].shape[-1]}, but should be {self.target_length}."
            )

        if batch["labels"].shape[-1] != self.target_length:
            raise ValueError(
                f"`labels` are incorrectly preprocessed. `labels` length is {batch['labels'].shape[-1]}, but should be {self.target_length}."
            )

        # to check that tokens are correctly preprocessed, one can run `self.tokenizer.batch_decode(input_ids)` and `self.tokenizer.batch_decode(labels)` here...
        batch["decoder_input_ids"] = shift_tokens_right(
            batch["labels"], self.pad_token_id, self.decoder_start_token_id
        )

        return batch

    def create_sentinel_ids(self, mask_indices):
        """
        Sentinel ids creation given the indices that should be masked.
        The start indices of each mask are replaced by the sentinel ids in increasing
        order. Consecutive mask indices to be deleted are replaced with `-1`.
        """
        start_indices = mask_indices - np.roll(mask_indices, 1, axis=-1) * mask_indices
        start_indices[:, 0] = mask_indices[:, 0]

        sentinel_ids = np.where(start_indices != 0, np.cumsum(start_indices, axis=-1), start_indices)
        sentinel_ids = np.where(sentinel_ids != 0, (len(self.tokenizer) - sentinel_ids), 0)
        sentinel_ids -= mask_indices - start_indices

        return sentinel_ids

    def filter_input_ids(self, input_ids, sentinel_ids):
        """
        Puts sentinel mask on `input_ids` and fuse consecutive mask tokens into a single mask token by deleting.
        This will reduce the sequence length from `expanded_inputs_length` to `input_length`.
        """
        batch_size = input_ids.shape[0]

        input_ids_full = np.where(sentinel_ids != 0, sentinel_ids, input_ids)
        # input_ids tokens and sentinel tokens are >= 0, tokens < 0 are
        # masked tokens coming after sentinel tokens and should be removed
        input_ids = input_ids_full[input_ids_full >= 0].reshape((batch_size, -1))
        input_ids = np.concatenate(
            [input_ids, np.full((batch_size, 1), self.tokenizer.eos_token_id, dtype=np.int32)], axis=-1
        )
        return input_ids

    def random_spans_noise_mask(self, length):

        """This function is copy of `random_spans_helper <https://github.com/google-research/text-to-text-transfer-transformer/blob/84f8bcc14b5f2c03de51bd3587609ba8f6bbd1cd/t5/data/preprocessors.py#L2682>`__ .
        Noise mask consisting of random spans of noise tokens.
        The number of noise tokens and the number of noise spans and non-noise spans
        are determined deterministically as follows:
        num_noise_tokens = round(length * noise_density)
        num_nonnoise_spans = num_noise_spans = round(num_noise_tokens / mean_noise_span_length)
        Spans alternate between non-noise and noise, beginning with non-noise.
        Subject to the above restrictions, all masks are equally likely.
        Args:
            length: an int32 scalar (length of the incoming token sequence)
            noise_density: a float - approximate density of output mask
            mean_noise_span_length: a number
        Returns:
            a boolean tensor with shape [length]
        """

        orig_length = length

        num_noise_tokens = int(np.round(length * self.noise_density))
        # avoid degeneracy by ensuring positive numbers of noise and nonnoise tokens.
        num_noise_tokens = min(max(num_noise_tokens, 1), length - 1)
        num_noise_spans = int(np.round(num_noise_tokens / self.mean_noise_span_length))

        # avoid degeneracy by ensuring positive number of noise spans
        num_noise_spans = max(num_noise_spans, 1)
        num_nonnoise_tokens = length - num_noise_tokens

        # pick the lengths of the noise spans and the non-noise spans
        def _random_segmentation(num_items, num_segments):
            """Partition a sequence of items randomly into non-empty segments.
            Args:
                num_items: an integer scalar > 0
                num_segments: an integer scalar in [1, num_items]
            Returns:
                a Tensor with shape [num_segments] containing positive integers that add
                up to num_items
            """
            mask_indices = np.arange(num_items - 1) < (num_segments - 1)
            np.random.shuffle(mask_indices)
            first_in_segment = np.pad(mask_indices, [[1, 0]])
            segment_id = np.cumsum(first_in_segment)
            # count length of sub segments assuming that list is sorted
            _, segment_length = np.unique(segment_id, return_counts=True)
            return segment_length

        noise_span_lengths = _random_segmentation(num_noise_tokens, num_noise_spans)
        nonnoise_span_lengths = _random_segmentation(num_nonnoise_tokens, num_noise_spans)

        interleaved_span_lengths = np.reshape(
            np.stack([nonnoise_span_lengths, noise_span_lengths], axis=1), [num_noise_spans * 2]
        )
        span_starts = np.cumsum(interleaved_span_lengths)[:-1]
        span_start_indicator = np.zeros((length,), dtype=np.int8)
        span_start_indicator[span_starts] = True
        span_num = np.cumsum(span_start_indicator)
        is_noise = np.equal(span_num % 2, 1)

        return is_noise[:orig_length]


def generate_batch_splits(samples_idx: jnp.ndarray, batch_size: int) -> jnp.ndarray:
    num_samples = len(samples_idx)
    samples_to_remove = num_samples % batch_size

    if samples_to_remove != 0:
        samples_idx = samples_idx[:-samples_to_remove]
    sections_split = num_samples // batch_size
    batch_idx = np.split(samples_idx, sections_split)
    return batch_idx


def write_train_metric(summary_writer, train_metrics, train_time, step):
    summary_writer.scalar("train_time", train_time, step)

    train_metrics = get_metrics(train_metrics)
    for key, vals in train_metrics.items():
        tag = f"train_{key}"
        for i, val in enumerate(vals):
            summary_writer.scalar(tag, val, step - len(vals) + i + 1)


def write_eval_metric(summary_writer, eval_metrics, step):
    for metric_name, value in eval_metrics.items():
        summary_writer.scalar(f"eval_{metric_name}", value, step)


# ## Arguments

# In[16]:


# parser = HfArgumentParser((ModelArguments, DataTrainingArguments, TrainingArguments))
# model_args, data_args, training_args = parser.parse_args_into_dataclasses()
model_args = ModelArguments()
data_args = DataArguments()
training_args = TrainingArguments()


# In[17]:


if (
    os.path.exists(training_args.output_dir)
    and os.listdir(training_args.output_dir)
    and training_args.do_train
    and not training_args.overwrite_output_dir
):
    raise ValueError(
        f"Output directory ({training_args.output_dir}) already exists and is not empty."
        "Use --overwrite_output_dir to overcome.")


# In[18]:


'''logging'''
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
    level=logging.INFO,
    datefmt="[%X]",
)
logger = logging.getLogger(__name__)
logger.info(f"Training/evaluation parameters {training_args}")

set_seed(training_args.seed)

if training_args.push_to_hub:
    if training_args.hub_model_id is None:
        repo_name = get_full_repo_name(Path(training_args.output_dir).absolute().name, token=training_args.hub_token)
    else:
        repo_name = training_args.hub_model_id
    repo = Repository(training_args.output_dir, clone_from=repo_name)


# ## Data

# ## Main

# ### Datasets

# In[19]:


if data_args.dataset_name is not None or data_args.data_files is not None:
    if data_args.dataset_name is not None:
        datasets = load_dataset(
            data_args.dataset_name,
            data_args.dataset_config_name,
            cache_dir=model_args.cache_dir,
            use_auth_token=model_args.use_auth_token,
        )
        if "validation" not in datasets.keys():
            datasets["validation"] = load_dataset(
                data_args.dataset_name,
                data_args.dataset_config_name,
                split=f"train[:{data_args.validation_split_percentage}%]",
                cache_dir=model_args.cache_dir,
                use_auth_token=True if model_args.use_auth_token else None,
            )
            datasets["train"] = load_dataset(
                data_args.dataset_name,
                data_args.dataset_config_name,
                split=f"train[{data_args.validation_split_percentage}%:]",
                cache_dir=model_args.cache_dir,
                use_auth_token=True if model_args.use_auth_token else None,
            )
    elif data_args.data_files is not None:
        datasets = load_dataset("json", data_files=data_args.data_files)
        datasets = datasets['train'].train_test_split(test_size=.2, shuffle=True, seed=training_args.seed)
        # TODO: just rename split_name
        datasets = DatasetDict({
            'train': datasets['train'],
            'validation': datasets['test'], 
        })
    else:
        raise NotImplementedError()
    


# In[20]:


datasets.keys()


# ### Models

# In[21]:


if model_args.tokenizer_name:
    tokenizer = AutoTokenizer.from_pretrained(
        model_args.tokenizer_name,
        cache_dir=model_args.cache_dir,
        use_fast=model_args.use_fast_tokenizer,
        use_auto_token=model_args.use_auth_token,
    )

if model_args.config_name:
    config = T5Config.from_pretrained(
        model_args.config_name,
        cache_dir=model_args.cache_dir,
        vocab_size=len(tokenizer),
        use_auth_token=model_args.use_auth_token,
    )


# ### Pre-processing

# In[22]:


if training_args.do_train:
    column_names = datasets["train"].column_names
else:
    column_names = datasets["validation"].column_names
text_column_name = "text" if "text" in column_names else column_names[0]

max_seq_length = min(data_args.max_seq_length, tokenizer.model_max_length)


# In[23]:


def tokenize_function(examples):
    return tokenizer(examples[text_column_name], return_attention_mask=False)

tokenized_datasets = datasets.map(
    tokenize_function,
    batched=True,
    num_proc=data_args.preprocessing_num_workers,
    remove_columns=column_names,
    load_from_cache_file=not data_args.overwrite_cache,
)


# In[24]:


expanded_inputs_length, targets_length = compute_input_and_target_lengths(
    inputs_length=max_seq_length,
    noise_density=data_args.mlm_probability,
    mean_noise_span_length=data_args.mean_noise_span_length,
)


# In[25]:


def group_texts(examples):
    # Concatenate all texts.
    concatenated_examples = {k: list(chain(*examples[k])) for k in examples.keys()}
    total_length = len(concatenated_examples[list(examples.keys())[0]])
    # We drop the small remainder, we could add padding if the model supported it instead of this drop, you can
    # customize this part to your needs.
    if total_length >= expanded_inputs_length:
        total_length = (total_length // expanded_inputs_length) * expanded_inputs_length
    # Split by chunks of max_len.
    result = {
        k: [t[i : i + expanded_inputs_length] for i in range(0, total_length, expanded_inputs_length)]
        for k, t in concatenated_examples.items()
    }
    return result


tokenized_datasets = tokenized_datasets.map(
    group_texts,
    batched=True,
    num_proc=data_args.preprocessing_num_workers,
    load_from_cache_file=not data_args.overwrite_cache,
)


# ### Tensorboard

# In[26]:


has_tensorboard = is_tensorboard_available()
if has_tensorboard and jax.process_index() == 0:
    try:
        from flax.metrics.tensorboard import SummaryWriter

        summary_writer = SummaryWriter(log_dir=Path(training_args.output_dir))
    except ImportError as ie:
        has_tensorboard = False
        logger.warning(
            f"Unable to display metrics through TensorBoard because some package are not installed: {ie}"
        )
else:
    logger.warning(
        "Unable to display metrics through TensorBoard because the package is not installed: "
        "Please run pip install tensorboard to enable."
    )


# ### initialize training

# In[27]:


rng = jax.random.PRNGKey(training_args.seed)
dropout_rngs = jax.random.split(rng, jax.local_device_count())

if model_args.model_name_or_path:
    model = FlaxT5ForConditionalGeneration.from_pretrained(
        model_args.model_name_or_path,
        config=config,
        seed=training_args.seed,
        dtype=getattr(jnp, model_args.dtype),
        use_auth_token=model_args.use_auth_token,
        from_pt=True,
    )


# In[28]:


data_collator = FlaxDataCollatorForT5MLM(
    tokenizer=tokenizer,
    noise_density=data_args.mlm_probability,
    mean_noise_span_length=data_args.mean_noise_span_length,
    input_length=max_seq_length,
    target_length=targets_length,
    pad_token_id=model.config.pad_token_id,
    decoder_start_token_id=model.config.decoder_start_token_id,
)


# In[29]:


num_epochs = int(training_args.num_train_epochs)
train_batch_size = int(training_args.per_device_train_batch_size) * jax.device_count()
eval_batch_size = int(training_args.per_device_eval_batch_size) * jax.device_count()

num_train_steps = len(tokenized_datasets["train"]) // train_batch_size * num_epochs

num_of_hosts = jax.process_count()
current_host_idx = jax.process_index()


# ### scheduler

# In[30]:


warmup_fn = optax.linear_schedule(
    init_value=0.0, end_value=training_args.learning_rate, transition_steps=training_args.warmup_steps
)
decay_fn = optax.linear_schedule(
    init_value=training_args.learning_rate,
    end_value=0,
    transition_steps=num_train_steps - training_args.warmup_steps,
)
linear_decay_lr_schedule_fn = optax.join_schedules(
    schedules=[warmup_fn, decay_fn], boundaries=[training_args.warmup_steps]
)


# In[31]:


def decay_mask_fn(params):
    flat_params = traverse_util.flatten_dict(params)
    flat_mask = {
        path: (path[-1] != "bias" and path[-2:] not in [("layer_norm", "scale"), ("final_layer_norm", "scale")])
        for path in flat_params
    }
    return traverse_util.unflatten_dict(flat_mask)


# ### optimizer

# In[32]:


if training_args.adafactor:
    optimizer = optax.adafactor(
        learning_rate=linear_decay_lr_schedule_fn,
    )
else:
    optimizer = optax.adamw(
        learning_rate=linear_decay_lr_schedule_fn,
        b1=training_args.adam_beta1,
        b2=training_args.adam_beta2,
        weight_decay=training_args.weight_decay,
        mask=decay_mask_fn,
    )


# In[33]:


state = train_state.TrainState.create(apply_fn=model.__call__, params=model.params, tx=optimizer)

def train_step(state, batch, dropout_rng):
    dropout_rng, new_dropout_rng = jax.random.split(dropout_rng)

    def loss_fn(params):
        labels = batch.pop("labels")
        logits = state.apply_fn(**batch, params=params, dropout_rng=dropout_rng, train=True)[0]
        loss = optax.softmax_cross_entropy(logits, onehot(labels, logits.shape[-1])).mean()
        return loss

    grad_fn = jax.value_and_grad(loss_fn)
    loss, grad = grad_fn(state.params)
    grad = jax.lax.pmean(grad, "batch")
    new_state = state.apply_gradients(grads=grad)
    metrics = jax.lax.pmean({
        "loss": loss, "learning_rate": linear_decay_lr_schedule_fn(state.step)
    }, axis_name="batch")
    return new_state, metrics, new_dropout_rng

p_train_step = jax.pmap(train_step, "batch", donate_argnums=(0, ))

def eval_step(params, batch):
    labels = batch.pop("labels")
    logits = model(**batch, params=params, train=False)[0]
    loss = optax.softmax_cross_entropy(logits, onehot(labels, logits.shape[-1]))
    accuracy = jnp.equal(jnp.argmax(logits, axis=-1), labels)
    metrics = {"loss": loss.mean(), "accuracy": accuracy.mean()}
    metrics = jax.lax.pmean(metrics, axis_name="batch")
    return metrics

p_eval_step = jax.pmap(eval_step, "batch", donate_argnums=(0, ))


# ### epoch

# In[ ]:


state = jax_utils.replicate(state)

train_time = 0
epochs = tqdm(range(num_epochs), desc="Epoch ... ", position=0)
for epoch in epochs:
    train_start = time.time()
    train_metrics = []

    rng, input_rng = jax.random.split(rng)
    num_train_samples = len(tokenized_datasets["train"])
    train_samples_idx = np.random.permutation(np.arange(num_train_samples))
    train_batch_idx = generate_batch_splits(train_samples_idx, train_batch_size)

    for step, batch_idx in enumerate(tqdm(train_batch_idx, desc="Training...", position=1)):
        samples = [tokenized_datasets["train"][int(idx)] for idx in batch_idx]
        model_inputs = data_collator(samples)

        local_host_model_inputs = {
            key: np.split(model_inputs.data[key], num_of_hosts, axis=0)[current_host_idx]
            for key, value in model_inputs.data.items()
        }
        # forward
        model_inputs = shard(local_host_model_inputs)
        state, train_metric, dropout_rngs = p_train_step(state, model_inputs, dropout_rngs)
        train_metrics.append(train_metric)

        cur_step = epoch * (num_train_samples // train_batch_size) + step
        if cur_step % training_args.logging_steps == 0 and cur_step > 0:
            train_metric = jax_utils.unreplicate(train_metric)
            train_time += time.time() - train_start
            if has_tensorboard and jax.process_index() == 0:
                write_train_metric(summary_writer, train_metrics, train_time, cur_step)
            epochs.write(f"Step... ({cur_step} | Loss: {train_metric['loss'].mean()}, Learning Rate: {train_metric['learning_rate'].mean()})")
            train_metrics = []

        if cur_step % training_args.eval_steps == 0 and cur_step > 0:
            num_eval_samples = len(tokenized_datasets["validation"])
            eval_samples_idx = jnp.arange(num_eval_samples)
            eval_batch_idx = generate_batch_splits(eval_samples_idx, eval_batch_size)

            eval_metrics = []
            for i, batch_idx in enumerate(tqdm(eval_batch_idx, desc="Evaluating...", position=2)):
                samples = [tokenized_datasets["validation"][int(idx)] for idx in batch_idx]
                model_inputs = data_collator(samples)
                # forward
                model_inputs = shard(model_inputs.data)
                metrics = p_eval_step(state.params, model_inputs)
                eval_metrics.append(metrics)
            eval_metrics = get_metrics(eval_metrics)
            eval_metrics = jax.tree_map(jnp.mean, eval_metrics)
            epochs.write(f"Step... ({cur_step} | Loss: {eval_metrics['loss']}, Acc: {eval_metrics['accuracy']})")
            if has_tensorboard and jax.process_index() == 0:
                write_eval_metric(summary_writer, eval_metrics, cur_step)
        
        if cur_step % training_args.save_steps == 0 and cur_step > 0:
            if jax.process_index() == 0:
                params = jax.device_get(jax.tree_map(lambda x: x[0], state.params))
                model.save_pretrained(training_args.output_dir, params=params)
                tokenizer.save_pretrained(training_args.output_dir)
                if training_args.push_to_hub:
                    repo.push_to_hub(commit_message=f"Saving weights and logs of step {cur_step}", blocking=False)


# In[ ]:





# ### evaluate

# In[ ]:



if training_args.do_eval:
    num_eval_samples = len(tokenized_datasets["validation"])
    eval_samples_idx = jnp.arange(num_eval_samples)
    eval_batch_idx = generate_batch_splits(eval_samples_idx, eval_batch_size)

    eval_metrics = []
    for i, batch_idx in enumerate(tqdm(eval_batch_idx, desc="Evaluating ...", position=2)):
        samples = [tokenized_datasets["validation"][int(idx)] for idx in batch_idx]
        model_inputs = data_collator(samples)

        # Model forward
        model_inputs = shard(model_inputs.data)
        metrics = p_eval_step(state.params, model_inputs)
        eval_metrics.append(metrics)

    # get eval metrics
    eval_metrics = get_metrics(eval_metrics)
    eval_metrics = jax.tree_map(lambda metric: jnp.mean(metric).item(), eval_metrics)

    if jax.process_index() == 0:
        eval_metrics = {f"eval_{metric_name}": value for metric_name, value in eval_metrics.items()}
        path = os.path.join(training_args.output_dir, "eval_results.json")
        with open(path, "w") as f:
            json.dump(eval_metrics, f, indent=4, sort_keys=True)

