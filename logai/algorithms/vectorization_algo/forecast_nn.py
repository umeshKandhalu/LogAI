import torch
import os
import numpy as np
import pickle as pkl
import pandas as pd
from attr import dataclass

from logai.algorithms.algo_interfaces import VectorizationAlgo
from .semantic import Semantic, SemanticVectorizerParams
from .sequential import Sequential, SequentialVectorizerParams
from logai.config_interfaces import Config
from logai.dataloader.data_model import LogRecordObject
from logai.utils import constants
from logai.algorithms.factory import factory


class ForecastNNVectorizedDataset:
    """class for storing vectorized dataset for forecasting based neural models 
    """

    session_idx: str = "session_idx"
    features: str = "features"
    window_anomalies: str = "window_anomalies"
    window_labels: str = "window_labels"

    def __init__(self, logline_features, labels, nextlogline_ids, span_ids):
        """initializing object for ForecastNNVectorizedDataset

        Args:
            logline_features (np.array): list of vectorized log-sequences 
            labels (list or pd.Series or np.array): list of labels (anomalous or non-anomalous) for each log sequence. 
            nextlogline_ids (list or pd.Series or np.array): list of ids of next loglines, for each log sequence
            span_ids (list or pd.Series or np.array): list of ids of log sequences. 
        """
        self.dataset = []
        for data_i, label_i, next_i, index_i in zip(
                logline_features, labels, nextlogline_ids, span_ids
        ):
            self.dataset.append(
                {
                    self.session_idx: np.array([index_i]),
                    self.features: np.array(data_i),
                    self.window_anomalies: label_i,
                    self.window_labels: next_i,
                }
            )


@dataclass
class ForecastNNVectorizerParams(Config):
    """Config class for vectorizer for forecast based neural models for log representation learning

    Inherits:
        Config : config interface
    """

    feature_type: str = None  # supported types "semantics" and "sequential"
    label_type: str = None
    sep_token: str = "[SEP]"
    max_token_len: int = None
    min_token_count: int = None
    embedding_dim: int = None
    output_dir: str = ""
    vectorizer_metadata_filepath: str = ""
    vectorizer_model_dirpath: str = ""

    sequentialvec_config: object = None
    semanticvec_config: object = None

    def from_dict(self, config_dict):
        super().from_dict(config_dict)


@factory.register("vectorization", "forecast_nn", ForecastNNVectorizerParams)
class ForecastNN(VectorizationAlgo):
    """Vectorizer Class for forecast based neural models for log representation learning
    """

    def __init__(self, config: ForecastNNVectorizerParams):
        """initializing vectorizer object for forecast based neural model

        Args:
            config (ForecastNNVectorizerParams): config object specifying parameters
             of forecast based neural log repersentation learning model 
        """
        self.meta_data = {}
        self.config = config
        sequentialvec_config = SequentialVectorizerParams()
        self.config.vectorizer_model_dirpath = os.path.join(
            self.config.output_dir, "embedding_model"
        )
        self.config.vectorizer_metadata_filepath = os.path.join(
            self.config.vectorizer_model_dirpath, "metadata.pkl"
        )

        if not os.path.exists(self.config.vectorizer_model_dirpath):
            os.makedirs(self.config.vectorizer_model_dirpath)

        sequentialvec_config.from_dict(
            {
                "sep_token": self.config.sep_token,
                "max_token_len": self.config.max_token_len,
                "model_save_dir": self.config.vectorizer_model_dirpath,
            }
        )
        self.sequential_vectorizer = Sequential(sequentialvec_config)
        self.semantic_vectorizer = None
        if self.config.feature_type == "semantics":
            semanticvec_config = SemanticVectorizerParams()
            semanticvec_config_dict = {
                "max_token_len": self.config.max_token_len,
                "min_token_count": self.config.min_token_count,
                "sep_token": self.config.sep_token,
                "embedding_dim": self.config.embedding_dim,
                "model_save_dir": self.config.vectorizer_model_dirpath,
            }
            semanticvec_config.from_dict(semanticvec_config_dict)
            self.semantic_vectorizer = Semantic(semanticvec_config)

    def _process_logsequence(self, data_sequence):
        data_sequence = data_sequence.dropna()
        unique_data_instances = pd.Series(
            list(
                set(
                    self.config.sep_token.join(list(data_sequence)).split(
                        self.config.sep_token
                    )
                )
            )
        )
        return unique_data_instances

    def fit(self, logrecord: LogRecordObject):
        """fit method to train vectorizer 

        Args:
            logrecord (LogRecordObject): logrecord object to train the vectorizer on
        """
        if self.sequential_vectorizer.vocab is None or (
                self.config.feature_type == "semantics"
                and self.semantic_vectorizer.vocab is None
        ):
            loglines = logrecord.body[
                constants.LOGLINE_NAME
            ]  # data[self.config.loglines_field]
            nextloglines = logrecord.attributes[
                constants.NEXT_LOGLINE_NAME
            ]  # data[self.config.nextlog_field]
            loglines = pd.concat([loglines, nextloglines])
            loglines = self._process_logsequence(loglines)
        if self.sequential_vectorizer.vocab is None:
            self.sequential_vectorizer.fit(loglines)
        if (
                self.config.feature_type == "semantics"
                and self.semantic_vectorizer.vocab is None
        ):
            self.semantic_vectorizer.fit(loglines)
        self._dump_meta_data()

    def transform(self, logrecord: LogRecordObject):
        """transform method to run vectorizer on logrecord object

        Args:
            logrecord (LogRecordObject): logrecord object to be vectorized

        Returns:
            ForecastNNVectorizedDataset: ForecastNNVectorizedDataset object
             containing the vectorized dataset
        """
        if self.config.feature_type == "sequential":
            logline_features = self.sequential_vectorizer.transform(
                logrecord.body[constants.LOGLINE_NAME]
            )
        elif self.config.feature_type == "semantics":
            logline_features = self.semantic_vectorizer.transform(
                logrecord.body[constants.LOGLINE_NAME]
            )
        if constants.NEXT_LOGLINE_NAME in logrecord.attributes:
            nextlogline_ids = self.sequential_vectorizer.transform(
                logrecord.attributes[constants.NEXT_LOGLINE_NAME]
            ).apply(lambda x: x[0])
        else:
            nextlogline_ids = None
        labels = logrecord.labels[constants.LABELS]
        span_ids = logrecord.span_id[constants.SPAN_ID]
        samples = ForecastNNVectorizedDataset(logline_features=logline_features,
                                              labels=labels, nextlogline_ids=nextlogline_ids, span_ids=span_ids)
        return samples

    def _dump_meta_data(self):
        if not os.path.exists(self.config.vectorizer_metadata_filepath):
            if self.config.feature_type == "sequential":
                self.meta_data["vocab_size"] = self.sequential_vectorizer.vocab_size
            else:
                self.meta_data["vocab_size"] = self.semantic_vectorizer.vocab_size
            if self.config.feature_type == "semantics":
                self.meta_data["pretrain_matrix"] = torch.Tensor(
                    self.semantic_vectorizer.embed_matrix
                )
            if self.config.label_type == "anomaly":
                self.meta_data["num_labels"] = 2
            else:
                self.meta_data["num_labels"] = self.sequential_vectorizer.vocab_size
            pkl.dump(
                self.meta_data, open(self.config.vectorizer_metadata_filepath, "wb")
            )
        else:
            self.meta_data = pkl.load(
                open(self.config.vectorizer_metadata_filepath, "rb")
            )
