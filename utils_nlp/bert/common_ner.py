# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.

import warnings

import torch
from pytorch_pretrained_bert.tokenization import BertTokenizer
import torch.nn as nn
from enum import Enum

from torch.utils.data import (
    DataLoader,
    RandomSampler,
    SequentialSampler,
    TensorDataset,
)
from torch.utils.data.distributed import DistributedSampler

# Max supported sequence length
BERT_MAX_LEN = 512


class Language(Enum):
    """An enumeration of the supported languages."""

    ENGLISH = "bert-base-uncased"
    ENGLISHCASED = "bert-base-cased"
    ENGLISHLARGE = "bert-large-uncased"
    ENGLISHLARGECASED = "bert-large-cased"
    CHINESE = "bert-base-chinese"
    MULTILINGUAL = "bert-base-multilingual-cased"


def get_device(device="gpu", num_devices=None):
    """Gets a PyTorch device.
    Args:
        device (str, optional): Device string: "cpu" or "gpu". Defaults to
        "gpu".
        num_devices (int, optional): Number of GPUs to be used. If None,
        all available GPUs are used. Default value is None
    Returns:
        tuple: A PyTorch device: cpu or gpu, number of GPUs to use
    """
    if device == "gpu":
        if torch.cuda.is_available():
            num_cuda_devices = torch.cuda.device_count()

            if num_devices is None:
                num_devices = num_cuda_devices
            else:
                if num_devices > num_cuda_devices:
                    num_devices = num_cuda_devices
                    warnings.warn(
                        "Only {} devices are available. Setting the number "
                        "of devices to {}".format(
                            num_cuda_devices, num_cuda_devices
                        )
                    )
            return torch.device("cuda:0"), num_devices
        raise Exception("CUDA device not available")
    elif device == "cpu":
        return torch.device("cpu"), 0
    else:
        raise Exception("Only 'cpu' and 'gpu' devices are supported.")


def parallelize_model(model, num_devices):
    """Implements model data parallelism on multiple GPUs.
    Args:
        model (PyTorch Module): A PyTorch model.
        num_devices (int): Number of GPUs to be used.
    Returns:
        [DataParallel, Module]: A PyTorch DataParallel module wrapper
                                or a PyTorch Module (if multiple CUDA
                                devices are not available).
    """

    if num_devices < 2:
        return model

    if not isinstance(model, nn.DataParallel):
        return nn.DataParallel(model, device_ids=list(range(num_devices)))
    else:
        return model


class Tokenizer:
    def __init__(
        self, language=Language.ENGLISH, to_lower=False, cache_dir="."
    ):
        """Initializes the tokenizer and the underlying pretrained tokenizer.
        Args:
            language (Language, optional): The pretrained model's language.
                                           Defaults to Language.ENGLISH.
            cache_dir (str, optional): Location of BERT's cache directory.
            Defaults to ".".
        """
        self.tokenizer = BertTokenizer.from_pretrained(
            language.value, do_lower_case=to_lower, cache_dir=cache_dir
        )
        self.language = language

    def tokenize(self, text):
        tokens = [self.tokenizer.tokenize(x) for x in text]
        return tokens

    def preprocess_classification_tokens(self, tokens, max_len):
        """Preprocessing of input tokens:
            - add BERT sentence markers ([CLS] and [SEP])
            - map tokens to indices
            - pad and truncate sequences
            - create an input_mask
        Args:
            tokens ([type]): List of tokens to preprocess.
            max_len ([type]): Maximum length of sequence.
        Returns:
            list of preprocesssed token lists
            list of input mask lists
        """
        if max_len > BERT_MAX_LEN:
            print(
                "setting max_len to max allowed tokens: {}".format(
                    BERT_MAX_LEN
                )
            )
            max_len = BERT_MAX_LEN

        # truncate and add BERT sentence markers
        tokens = [["[CLS]"] + x[0 : max_len - 2] + ["[SEP]"] for x in tokens]
        # convert tokens to indices
        tokens = [self.tokenizer.convert_tokens_to_ids(x) for x in tokens]
        # pad sequence
        tokens = [x + [0] * (max_len - len(x)) for x in tokens]
        # create input mask
        input_mask = [[min(1, x) for x in y] for y in tokens]
        return tokens, input_mask

    def preprocess_ner_tokens(
        self,
        text,
        max_seq_length=BERT_MAX_LEN,
        labels=None,
        label_map=None,
        trailing_piece_tag="X",
    ):
        """
        Preprocesses input tokens, involving the following steps
            1. Convert input token to token ids
            2. Convert input labels to label ids, if labels and label_map are
                provided.
            3. If a word is tokenized into multiple pieces of tokens by the
                WordPiece tokenizer, label the extra tokens with
                trailing_piece_tag.
            4. Pad or truncate input text according to max_seq_length
            5. Create input_mask for masking out padded tokens.

        Args:
            text (list): List of input sentences/paragraphs.
            max_seq_length (int, optional): Maximum length of the list of
                tokens. Lists longer than this are truncated and shorter
                ones are padded with "O"s. Default value is BERT_MAX_LEN=512.
            labels (list, optional): List of token label lists. Default
                value is None.
            label_map (dict, optional): Dictionary for mapping original token
                labels (which may be string type) to integers. Default value
                is None.
            trailing_piece_tag (str, optional): Tags used to label trailing
                word pieces. For example, "playing" is broken into "play"
                and "##ing", "play" preserves its original label and "##ing"
                is labeled as trailing_piece_tag. Default value is "X".

        Returns:
            tuple: A tuple containing the following three or four lists.
                1. input_ids_all: List of lists. Each sublist contains
                    numerical values, i.e. token ids, corresponding to the
                    tokens in the input text data.
                2. input_mask_all: List of lists. Each sublist
                    contains the attention mask of the input token id list,
                    1 for input tokens and 0 for padded tokens, so that
                    padded tokens are not attended to.
                3. trailing_token_mask: List of lists. Each sublist is
                    a boolean list, True for the first word piece of each
                    original word, False for the trailing word pieces,
                    e.g. "##ing". This mask is useful for removing the
                    predictions on trailing word pieces, so that each
                    original word in the input text has a unique predicted
                    label.
                4. label_ids_all: List of lists of numerical labels,
                    each sublist contains token labels of a input
                    sentence/paragraph, if labels is provided.
        """
        if max_seq_length > BERT_MAX_LEN:
            warnings.warn(
                "setting max_len to max allowed tokens: {}".format(
                    BERT_MAX_LEN
                )
            )
            max_seq_length = BERT_MAX_LEN

        label_available = True
        if labels is None:
            label_available = False
            # create an artificial label list for creating trailing token mask
            labels = ["O"] * len(text)

        input_ids_all = []
        input_mask_all = []
        label_ids_all = []
        trailing_token_mask_all = []
        for t, t_labels in zip(text, labels):
            text_lower = t.lower()

            new_labels = []
            tokens = []
            for word, tag in zip(text_lower.split(), t_labels):
                sub_words = self.tokenizer.wordpiece_tokenizer.tokenize(word)
                for count, sub_word in enumerate(sub_words):
                    if count > 0:
                        tag = trailing_piece_tag
                    new_labels.append(tag)
                    tokens.append(sub_word)

            if len(tokens) > max_seq_length:
                tokens = tokens[:max_seq_length]
                new_labels = new_labels[:max_seq_length]

            input_ids = self.tokenizer.convert_tokens_to_ids(tokens)

            # The mask has 1 for real tokens and 0 for padding tokens.
            # Only real tokens are attended to.
            input_mask = [1.0] * len(input_ids)

            # Zero-pad up to the max sequence length.
            padding = [0.0] * (max_seq_length - len(input_ids))
            label_padding = ["O"] * (max_seq_length - len(input_ids))

            input_ids += padding
            input_mask += padding
            new_labels += label_padding

            trailing_token_mask_all.append(
                [
                    True if label != trailing_piece_tag else False
                    for label in new_labels
                ]
            )

            if label_map:
                label_ids = [label_map[label] for label in new_labels]
            else:
                label_ids = new_labels

            input_ids_all.append(input_ids)
            input_mask_all.append(input_mask)
            label_ids_all.append(label_ids)

        if label_available:
            return (
                input_ids_all,
                input_mask_all,
                trailing_token_mask_all,
                label_ids_all,
            )
        else:
            return input_ids_all, input_mask_all, trailing_token_mask_all


def create_data_loader(
    input_ids,
    input_mask,
    label_ids=None,
    sample_method="random",
    batch_size=32,
):
    """
    Create a dataloader for sampling and serving data batches.
    Args:
        input_ids (list): List of lists. Each sublist contains numerical
            values, i.e. token ids, corresponding to the tokens in the input
            text data.
        input_mask (list): List of lists. Each sublist contains the attention
            mask of the input token id list, 1 for input tokens and 0 for
            padded tokens, so that padded tokens are not attended to.
        label_ids (list, optional): List of lists of numerical labels,
            each sublist contains token labels of a input
            sentence/paragraph. Default value is None.
        sample_method (str, optional): Order of data sampling. Accepted
            values are "random", "sequential" and "distributed". Default
            value is "random".
        batch_size (int, optional): Number of samples used in each training
            iteration. Default value is 32.

    Returns:
        DataLoader: A Pytorch Dataloader containing the input_ids tensor,
            input_mask tensor, and label_ids (if provided) tensor.

    """
    input_ids_tensor = torch.tensor(input_ids, dtype=torch.long)
    input_mask_tensor = torch.tensor(input_mask, dtype=torch.long)

    if label_ids:
        label_ids_tensor = torch.tensor(label_ids, dtype=torch.long)
        tensor_data = TensorDataset(
            input_ids_tensor, input_mask_tensor, label_ids_tensor
        )
    else:
        tensor_data = TensorDataset(input_ids_tensor, input_mask_tensor)

    if sample_method == "random":
        sampler = RandomSampler(tensor_data)
    elif sample_method == "sequential":
        sampler = SequentialSampler(tensor_data)
    elif sample_method == "distributed":
        sampler = DistributedSampler(tensor_data)
    else:
        raise ValueError(
            "Invalid sample_method value, accepted values are: "
            "random, sequential, and distributed"
        )

    dataloader = DataLoader(
        tensor_data, sampler=sampler, batch_size=batch_size
    )

    return dataloader
