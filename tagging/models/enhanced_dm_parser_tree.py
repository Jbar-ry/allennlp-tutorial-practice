# source: https://github.com/allenai/allennlp-models/blob/master/allennlp_models/structured_prediction/models/graph_parser.py
# modified by James Barry, Dublin City University
# Licence: Apache License 2.0

"""
This model is based on the original AllenNLP implementation: https://github.com/allenai/allennlp-models/blob/master/allennlp_models/structured_prediction/models/graph_parser.py
"""

from typing import Dict, Tuple, Any, List
import logging
import copy
from operator import itemgetter

from overrides import overrides
import torch
from torch.nn.modules import Dropout
import numpy

from allennlp.common.checks import check_dimensions_match, ConfigurationError
from allennlp.data import TextFieldTensors, Vocabulary
from allennlp.modules import Seq2SeqEncoder, TextFieldEmbedder, Embedding, InputVariationalDropout
from allennlp.modules.matrix_attention.bilinear_matrix_attention import BilinearMatrixAttention
from allennlp.modules import FeedForward
from allennlp.models.model import Model
from allennlp.nn import InitializerApplicator, Activation
from allennlp.nn.util import min_value_of_dtype
from allennlp.nn.util import get_text_field_mask
from allennlp.nn.util import get_lengths_from_binary_sequence_mask
from tagging.training.enhanced_attachment_scores import EnhancedAttachmentScores

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


@Model.register("enhanced_dm_parser_tree")
class EnhancedDMParserTree(Model):
    """
    A Parser for arbitrary graph structures.
    This enhanced dependency parser follows the model of
    ` Deep Biaffine Attention for Neural Dependency Parsing (Dozat and Manning, 2016)
    <https://arxiv.org/abs/1611.01734>`_ .

    Registered as a `Model` with name "graph_parser".
    # Parameters
    vocab : `Vocabulary`, required
        A Vocabulary, required in order to compute sizes for input/output projections.
    text_field_embedder : `TextFieldEmbedder`, required
        Used to embed the `tokens` `TextField` we get as input to the model.
    encoder : `Seq2SeqEncoder`
        The encoder (with its own internal stacking) that we will use to generate representations
        of tokens.
    tag_representation_dim : `int`, required.
        The dimension of the MLPs used for arc tag prediction.
    arc_representation_dim : `int`, required.
        The dimension of the MLPs used for arc prediction.
    tag_feedforward : `FeedForward`, optional, (default = None).
        The feedforward network used to produce tag representations.
        By default, a 1 layer feedforward network with an elu activation is used.
    arc_feedforward : `FeedForward`, optional, (default = None).
        The feedforward network used to produce arc representations.
        By default, a 1 layer feedforward network with an elu activation is used.
    pos_tag_embedding : `Embedding`, optional.
        Used to embed the `pos_tags` `SequenceLabelField` we get as input to the model.
    dropout : `float`, optional, (default = 0.0)
        The variational dropout applied to the output of the encoder and MLP layers.
    input_dropout : `float`, optional, (default = 0.0)
        The dropout applied to the embedded text input.
    edge_prediction_threshold : `int`, optional (default = 0.5)
        The probability at which to consider a scored edge to be 'present'
        in the decoded graph. Must be between 0 and 1.
    initializer : `InitializerApplicator`, optional (default=`InitializerApplicator()`)
        Used to initialize the model parameters.
    """

    def __init__(
        self,
        vocab: Vocabulary,
        text_field_embedder: TextFieldEmbedder,
        encoder: Seq2SeqEncoder,
        tag_representation_dim: int,
        arc_representation_dim: int,
        tag_feedforward: FeedForward = None,
        arc_feedforward: FeedForward = None,
        lemma_tag_embedding: Embedding = None,
        upos_tag_embedding: Embedding = None,
        xpos_tag_embedding: Embedding = None,
        feats_tag_embedding: Embedding = None,
        head_information_embedding: Embedding = None,
        head_tag_embedding: Embedding = None,
        dropout: float = 0.0,
        input_dropout: float = 0.0,
        edge_prediction_threshold: float = 0.5,
        initializer: InitializerApplicator = InitializerApplicator(),
        **kwargs,
    ) -> None:
        super().__init__(vocab, **kwargs)

        self.text_field_embedder = text_field_embedder
        self.encoder = encoder
        self.edge_prediction_threshold = edge_prediction_threshold
        if not 0 < edge_prediction_threshold < 1:
            raise ConfigurationError(f"edge_prediction_threshold must be between "
                                     f"0 and 1 (exclusive) but found {edge_prediction_threshold}.")

        encoder_dim = encoder.get_output_dim()

        self.head_arc_feedforward = arc_feedforward or FeedForward(
            encoder_dim, 1, arc_representation_dim, Activation.by_name("elu")()
        )
        self.child_arc_feedforward = copy.deepcopy(self.head_arc_feedforward)

        self.arc_attention = BilinearMatrixAttention(
            arc_representation_dim, arc_representation_dim, use_input_biases=True
        )

        num_labels = self.vocab.get_vocab_size("deps")
        self.head_tag_feedforward = tag_feedforward or FeedForward(
            encoder_dim, 1, tag_representation_dim, Activation.by_name("elu")()
        )
        self.child_tag_feedforward = copy.deepcopy(self.head_tag_feedforward)

        self.tag_bilinear = BilinearMatrixAttention(
            tag_representation_dim, tag_representation_dim, label_dim=num_labels
        )

        self._lemma_tag_embedding = lemma_tag_embedding or None
        self._upos_tag_embedding = upos_tag_embedding or None
        self._xpos_tag_embedding = xpos_tag_embedding or None
        self._feats_tag_embedding = feats_tag_embedding or None
        self._head_tag_embedding = head_tag_embedding or None
        self._head_information_embedding = head_information_embedding or None

        self._dropout = InputVariationalDropout(dropout)
        self._input_dropout = Dropout(input_dropout)

        # add a head sentinel to accommodate for extra root token in EUD graphs
        self._head_sentinel = torch.nn.Parameter(torch.randn([1, 1, encoder.get_output_dim()]))

        representation_dim = text_field_embedder.get_output_dim()
        if lemma_tag_embedding is not None:
            representation_dim += lemma_tag_embedding.get_output_dim()
        if upos_tag_embedding is not None:
            representation_dim += upos_tag_embedding.get_output_dim()
        if xpos_tag_embedding is not None:
            representation_dim += xpos_tag_embedding.get_output_dim()
        if feats_tag_embedding is not None:
            representation_dim += feats_tag_embedding.get_output_dim()
        if head_tag_embedding is not None:
            representation_dim += head_tag_embedding.get_output_dim()
        if head_information_embedding is not None:
            representation_dim += head_information_embedding.get_output_dim()

        check_dimensions_match(
            representation_dim,
            encoder.get_input_dim(),
            "text field embedding dim",
            "encoder input dim",
        )
        check_dimensions_match(
            tag_representation_dim,
            self.head_tag_feedforward.get_output_dim(),
            "tag representation dim",
            "tag feedforward output dim",
        )
        check_dimensions_match(
            arc_representation_dim,
            self.head_arc_feedforward.get_output_dim(),
            "arc representation dim",
            "arc feedforward output dim",
        )

        self._enhanced_attachment_scores = EnhancedAttachmentScores()
        self._arc_loss = torch.nn.BCEWithLogitsLoss(reduction="none")
        self._tag_loss = torch.nn.CrossEntropyLoss(reduction="none")
        initializer(self)

    @overrides
    def forward(
        self,  # type: ignore
        tokens: TextFieldTensors,
        lemmas: torch.LongTensor = None,
        upos: torch.LongTensor = None,
        xpos: torch.LongTensor = None,
        feats: torch.LongTensor = None,
        deprels: torch.LongTensor = None,
        heads: torch.LongTensor = None,
        enhanced_tags: torch.LongTensor = None,
        metadata: List[Dict[str, Any]] = None,
    ) -> Dict[str, torch.Tensor]:

        """
        # Parameters
        tokens : TextFieldTensors, required
            The output of `TextField.as_array()`.
        pos_tags : torch.LongTensor, optional (default = None)
            The output of a `SequenceLabelField` containing POS tags.
        metadata : List[Dict[str, Any]], optional (default = None)
            A dictionary of metadata for each batch element which has keys:
                tokens : `List[str]`, required.
                    The original string tokens in the sentence.
        enhanced_tags : torch.LongTensor, optional (default = None)
            A torch tensor representing the sequence of integer indices denoting the parent of every
            word in the dependency parse. Has shape ``(batch_size, sequence_length, sequence_length)``.

        # Returns

        An output dictionary.
        """
        embedded_text_input = self.text_field_embedder(tokens)
        concatenated_input = [embedded_text_input]
        if upos is not None and self._upos_tag_embedding is not None:
            concatenated_input.append(self._upos_tag_embedding(upos))
        elif self._upos_tag_embedding is not None:
            raise ConfigurationError("Model uses a POS embedding, but no POS tags were passed.")

        if lemmas is not None and self._lemma_tag_embedding is not None:
            concatenated_input.append(self._lemma_tag_embedding(lemmas))
        if xpos is not None and self._xpos_tag_embedding is not None:
            concatenated_input.append(self._xpos_tag_embedding(xpos))
        if feats is not None and self._feats_tag_embedding is not None:
            batch_size, sequence_len, max_len = feats.size()
            # shape: (batch, seq_len, max_len)
            feats_mask = (feats != -1).long()
            feats = feats * feats_mask
            # tensor corresponding to the number of active components, e.g. morphological features
            number_active_components = feats_mask.sum(-1)
            # a padding token's summed vector will be filled with 0s and when this is divided by 0
            # it will return a NaN so we replaces 0s with 1s in the denominator tensor to avoid this.
            number_active_components[number_active_components==0] = 1

            feats_embeddings = []
            # shape: (seq_len, max_len)
            for feat_tensor in feats:
                # shape: (seq_len, max_len, emb_dim)
                embedded_feats = self._feats_tag_embedding(feat_tensor)
                feats_embeddings.append(embedded_feats)
            # shape: (batch, seq_len, max_len, emb_dim)
            stacked_feats_tensor = torch.stack(feats_embeddings)
            tag_embedding_dim = stacked_feats_tensor.size(-1)
            feats_mask_expanded = feats_mask.unsqueeze_(-1).expand(batch_size, sequence_len, max_len, tag_embedding_dim)
            # shape: (batch, seq_len, max_len, emb_dim)
            masked_feats = stacked_feats_tensor * feats_mask_expanded
            # shape: (batch, seq_len, tag_embedding_dim)
            combined_masked_feats = masked_feats.sum(2)
            expanded_number_active_components = number_active_components.unsqueeze(-1).expand(batch_size, sequence_len, tag_embedding_dim)
            # divide the summed feats vectors by the number of non-padded elements
            averaged_feats = combined_masked_feats / expanded_number_active_components
            concatenated_input.append(averaged_feats)

        if deprels is not None and self._head_tag_embedding is not None:
            concatenated_input.append(self._head_tag_embedding(deprels))

        # TODO BASIC TREE
        if heads is not None and self._head_information_embedding is not None:
            batch_size, sequence_len, max_len = heads.size()
            # shape: (batch, seq_len, max_len)
            head_information_mask = (heads != -1).long()
            heads = heads * head_information_mask
            # tensor corresponding to the number of active components, e.g. morphological features
            number_active_components = head_information_mask.sum(-1)
            # a padding token's summed vector will be filled with 0s and when this is divided by 0
            # it will return a NaN so we replaces 0s with 1s in the denominator tensor to avoid this.
            number_active_components[number_active_components==0] = 1

            head_information_embeddings = []
            # shape: (seq_len, max_len)
            for head_information_tensor in heads:
                # shape: (seq_len, max_len, emb_dim)
                embedded_head_information = self._head_information_embedding(head_information_tensor)
                head_information_embeddings.append(embedded_head_information)
            # shape: (batch, seq_len, max_len, emb_dim)
            stacked_head_information_tensor = torch.stack(head_information_embeddings)
            tag_embedding_dim = stacked_head_information_tensor.size(-1)
            head_information_mask_expanded = head_information_mask.unsqueeze_(-1).expand(batch_size, sequence_len, max_len, tag_embedding_dim)
            # shape: (batch, seq_len, max_len, emb_dim)
            masked_head_information = stacked_head_information_tensor * head_information_mask_expanded
            # shape: (batch, seq_len, tag_embedding_dim)
            combined_masked_head_information = masked_head_information.sum(2)
            expanded_number_active_components = number_active_components.unsqueeze(-1).expand(batch_size, sequence_len, tag_embedding_dim)
            # divide the summed head information vectors by the number of non-padded elements
            averaged_head_information = combined_masked_head_information / expanded_number_active_components
            concatenated_input.append(averaged_head_information)


        if len(concatenated_input) > 1:
            embedded_text_input = torch.cat(concatenated_input, -1)

        mask = get_text_field_mask(tokens)
        embedded_text_input = self._input_dropout(embedded_text_input)
        encoded_text = self.encoder(embedded_text_input, mask)

        batch_size, _, encoding_dim = encoded_text.size()

        head_sentinel = self._head_sentinel.expand(batch_size, 1, encoding_dim)
        # Concatenate the head sentinel onto the sentence representation.
        encoded_text = torch.cat([head_sentinel, encoded_text], 1)
        mask = torch.cat([mask.new_ones(batch_size, 1), mask], 1)
        encoded_text = self._dropout(encoded_text)

        # shape (batch_size, sequence_length, arc_representation_dim)
        head_arc_representation = self._dropout(self.head_arc_feedforward(encoded_text))
        child_arc_representation = self._dropout(self.child_arc_feedforward(encoded_text))

        # shape (batch_size, sequence_length, tag_representation_dim)
        head_tag_representation = self._dropout(self.head_tag_feedforward(encoded_text))
        child_tag_representation = self._dropout(self.child_tag_feedforward(encoded_text))

        # shape (batch_size, sequence_length, sequence_length)
        arc_scores = self.arc_attention(head_arc_representation, child_arc_representation)

        # shape (batch_size, num_tags, sequence_length, sequence_length)
        arc_tag_logits = self.tag_bilinear(head_tag_representation, child_tag_representation)

        # Switch to (batch_size, sequence_length, sequence_length, num_tags)
        arc_tag_logits = arc_tag_logits.permute(0, 2, 3, 1).contiguous()

        # Since we'll be doing some additions, using the min value will cause underflow
        minus_mask = ~mask * min_value_of_dtype(arc_scores.dtype) / 10
        arc_scores = arc_scores + minus_mask.unsqueeze(2) + minus_mask.unsqueeze(1)

        arc_probs, arc_tag_probs = self._greedy_decode(arc_scores, arc_tag_logits, mask)

        output_dict = {"arc_probs": arc_probs, "arc_tag_probs": arc_tag_probs, "mask": mask}

        if metadata:
            output_dict["conllu_metadata"] = [meta["conllu_metadata"] for meta in metadata]
            output_dict["ids"] = [meta["ids"] for meta in metadata]
            output_dict["tokens"] = [meta["tokens"] for meta in metadata]
            output_dict["lemmas"] = [meta["lemmas"] for meta in metadata]
            output_dict["upos"] = [meta["upos_tags"] for meta in metadata]
            output_dict["xpos"] = [meta["xpos_tags"] for meta in metadata]
            output_dict["feats"] = [meta["feats"] for meta in metadata]
            output_dict["head_tags"] = [meta["head_tags"] for meta in metadata]
            output_dict["head_indices"] = [meta["head_indices"] for meta in metadata]
            output_dict["original_to_new_indices"] = [meta["original_to_new_indices"] for meta in metadata]
            output_dict["misc"] = [meta["misc"] for meta in metadata]
            output_dict["multiword_ids"] = [x["multiword_ids"] for x in metadata if "multiword_ids" in x]
            output_dict["multiword_forms"] = [x["multiword_forms"] for x in metadata if "multiword_forms" in x]

        if enhanced_tags is not None:
            arc_nll, tag_nll = self._construct_loss(
                arc_scores=arc_scores, arc_tag_logits=arc_tag_logits, enhanced_tags=enhanced_tags, mask=mask
            )

            output_dict["loss"] = arc_nll + tag_nll
            output_dict["arc_loss"] = arc_nll
            output_dict["tag_loss"] = tag_nll

            # get human readable output to computed enhanced graph metrics
            output_dict = self.make_output_human_readable(output_dict)

            # predicted arcs, arc_tags
            predicted_arcs = output_dict["arcs"]
            predicted_arc_tags = output_dict["arc_tags"]
            predicted_labeled_arcs = output_dict["labeled_arcs"]

            # gold arcs, arc_tags
            gold_arcs = [meta["arc_indices"] for meta in metadata]
            gold_arc_tags = [meta["arc_tags"] for meta in metadata]
            gold_labeled_arcs = [meta["labeled_arcs"] for meta in metadata]

            tag_mask = mask.unsqueeze(1) & mask.unsqueeze(2)
            self._enhanced_attachment_scores(predicted_arcs, predicted_arc_tags, predicted_labeled_arcs, \
                                             gold_arcs, gold_arc_tags, gold_labeled_arcs, tag_mask)

        return output_dict


    @overrides
    def make_output_human_readable(
        self, output_dict: Dict[str, torch.Tensor]
    ) -> Dict[str, torch.Tensor]:
        arc_tag_probs = output_dict["arc_tag_probs"].cpu().detach().numpy()
        arc_probs = output_dict["arc_probs"].cpu().detach().numpy()
        mask = output_dict["mask"]
        lengths = get_lengths_from_binary_sequence_mask(mask)
        arcs = []
        arc_tags = []
        # append arc and label to calculate ELAS
        labeled_arcs = []

        for instance_arc_probs, instance_arc_tag_probs, length in zip(
            arc_probs, arc_tag_probs, lengths
        ):
            arc_matrix = instance_arc_probs > self.edge_prediction_threshold
            edges = []
            edge_tags = []
            edges_and_tags = []
            # dictionary where a word has been assigned a head
            found_heads = {}
            # Note: manually selecting the most probable edge will result in slightly different F1 scores
            # between F1Measure and EnhancedAttachmentScores
            # set each label to False but will be updated as True if the word has a head over the threshold
            for i in range(length):
                found_heads[i] = False

            for i in range(length):
                for j in range(length):
                    if arc_matrix[i, j] == 1:
                        head_modifier_tuple = (i, j)
                        edges.append(head_modifier_tuple)
                        tag = instance_arc_tag_probs[i, j].argmax(-1)
                        edge_tags.append(self.vocab.get_token_from_index(tag, "deps"))
                        # append ((h,m), label) tuple
                        edges_and_tags.append((head_modifier_tuple, self.vocab.get_token_from_index(tag, "deps")))
                        found_heads[j] = True

            # some words won't have found heads so we will find the edge with the highest probability for each unassigned word
            head_information = found_heads.items()
            unassigned_tokens = []
            for (word, has_found_head) in head_information:
                # we're not interested in selecting heads for the dummy ROOT token
                if has_found_head == False and word != 0:
                    unassigned_tokens.append(word)

            if len(unassigned_tokens) >= 1:
                head_choices = {unassigned_token: [] for unassigned_token in unassigned_tokens}

                # keep track of the probabilities of the other words being heads of the unassigned tokens
                for i in range(length):
                    for j in unassigned_tokens:
                        # edge
                        head_modifier_tuple = (i, j)
                        # score
                        probability = instance_arc_probs[i, j]
                        head_choices[j].append((head_modifier_tuple, probability))

                for unassigned_token, edge_score_tuples in head_choices.items():
                    # get the best edge for each unassigned token based on the score which is element [1] in the tuple.
                    best_edge = max(edge_score_tuples, key = itemgetter(1))[0]

                    edges.append(best_edge)
                    tag = instance_arc_tag_probs[best_edge].argmax(-1)
                    edge_tags.append(self.vocab.get_token_from_index(tag, "deps"))
                    edges_and_tags.append((best_edge, self.vocab.get_token_from_index(tag, "deps")))

            arcs.append(edges)
            arc_tags.append(edge_tags)
            labeled_arcs.append(edges_and_tags)

        output_dict["arcs"] = arcs
        output_dict["arc_tags"] = arc_tags
        output_dict["labeled_arcs"] = labeled_arcs
        return output_dict

    def _construct_loss(
        self,
        arc_scores: torch.Tensor,
        arc_tag_logits: torch.Tensor,
        enhanced_tags: torch.Tensor,
        mask: torch.BoolTensor,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Computes the arc and tag loss for an adjacency matrix.

        # Parameters

        arc_scores : `torch.Tensor`, required.
            A tensor of shape (batch_size, sequence_length, sequence_length) used to generate a
            binary classification decision for whether an edge is present between two words.
        arc_tag_logits : `torch.Tensor`, required.
            A tensor of shape (batch_size, sequence_length, sequence_length, num_tags) used to generate
            a distribution over edge tags for a given edge.
        enhanced_tags : `torch.Tensor`, required.
            A tensor of shape (batch_size, sequence_length, sequence_length).
            The labels for every arc.
        mask : `torch.BoolTensor`, required.
            A mask of shape (batch_size, sequence_length), denoting unpadded
            elements in the sequence.
        # Returns
        arc_nll : `torch.Tensor`, required.
            The negative log likelihood from the arc loss.
        tag_nll : `torch.Tensor`, required.
            The negative log likelihood from the arc tag loss.
        """
        arc_indices = (enhanced_tags != -1).float()
        # Make the arc tags not have negative values anywhere
        # (by default, no edge is indicated with -1).
        enhanced_tags = enhanced_tags * arc_indices
        arc_nll = self._arc_loss(arc_scores, arc_indices) * mask.unsqueeze(1) * mask.unsqueeze(2)
        # We want the mask for the tags to only include the unmasked words
        # and we only care about the loss with respect to the gold arcs.
        # tag_mask: (batch, sequence_length, sequence_length)
        tag_mask = mask.unsqueeze(1) * mask.unsqueeze(2) * arc_indices
        batch_size, sequence_length, _, num_tags = arc_tag_logits.size()
        original_shape = [batch_size, sequence_length, sequence_length]
        # reshaped_logits: (batch * sequence_length * sequence_length, num_tags)
        reshaped_logits = arc_tag_logits.view(-1, num_tags)
        # reshaped_tags: (batch * sequence_length * sequence_length)
        reshaped_tags = enhanced_tags.view(-1)
        tag_nll = (
            self._tag_loss(reshaped_logits, reshaped_tags.long()).view(original_shape) * tag_mask
        )
        valid_positions = tag_mask.sum()

        arc_nll = arc_nll.sum() / valid_positions.float()
        tag_nll = tag_nll.sum() / valid_positions.float()
        return arc_nll, tag_nll

    @staticmethod
    def _greedy_decode(
        arc_scores: torch.Tensor, arc_tag_logits: torch.Tensor, mask: torch.BoolTensor
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Decodes the head and head tag predictions by decoding the unlabeled arcs
        independently for each word and then again, predicting the head tags of
        these greedily chosen arcs independently.

        # Parameters

        arc_scores : `torch.Tensor`, required.
            A tensor of shape (batch_size, sequence_length, sequence_length) used to generate
            a distribution over attachments of a given word to all other words.
        arc_tag_logits : `torch.Tensor`, required.
            A tensor of shape (batch_size, sequence_length, sequence_length, num_tags) used to
            generate a distribution over tags for each arc.
        mask : `torch.BoolTensor`, required.
            A mask of shape (batch_size, sequence_length).

        # Returns

        arc_probs : `torch.Tensor`
            A tensor of shape (batch_size, sequence_length, sequence_length) representing the
            probability of an arc being present for this edge.
        arc_tag_probs : `torch.Tensor`
            A tensor of shape (batch_size, sequence_length, sequence_length, sequence_length)
            representing the distribution over edge tags for a given edge.
        """
        # Mask the diagonal, because we don't self edges.
        inf_diagonal_mask = torch.diag(arc_scores.new(mask.size(1)).fill_(-numpy.inf))
        arc_scores = arc_scores + inf_diagonal_mask
        # shape (batch_size, sequence_length, sequence_length, num_tags)
        arc_tag_logits = arc_tag_logits + inf_diagonal_mask.unsqueeze(0).unsqueeze(-1)
        # Mask padded tokens, because we only want to consider actual word -> word edges.
        minus_mask = ~mask.unsqueeze(2)
        arc_scores.masked_fill_(minus_mask, -numpy.inf)
        arc_tag_logits.masked_fill_(minus_mask.unsqueeze(-1), -numpy.inf)
        # shape (batch_size, sequence_length, sequence_length)
        arc_probs = arc_scores.sigmoid()
        # shape (batch_size, sequence_length, sequence_length, num_tags)
        arc_tag_probs = torch.nn.functional.softmax(arc_tag_logits, dim=-1)
        return arc_probs, arc_tag_probs

    @overrides
    def get_metrics(self, reset: bool = False) -> Dict[str, float]:
        metrics = {}
        metrics_to_track = ["unlabeled_f1", "labeled_f1"]

        graph_results_dict = self._enhanced_attachment_scores.get_metric(reset)
        for metric, value in graph_results_dict.items():
            if metric in metrics_to_track:
                metrics[metric] = value

        return metrics
