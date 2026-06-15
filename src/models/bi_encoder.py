"""Siamese Bi-Encoder: shared transformer + scalar risk score per alert."""
from __future__ import annotations

import torch
import torch.nn as nn
from transformers import AutoModel, AutoTokenizer


class BiEncoderRiskModel(nn.Module):
    """Encode alert text → scalar score. Pairwise training via BCE on score_A - score_B.

    Architecture (per Lev Muchnik feedback 2026-06-08):
        BERT [CLS] (768 dim)
            → Dropout(dropout)
            → Linear(768 → hidden_size)  # 100 neurons
            → ReLU
            → Dropout(dropout)
            → Linear(hidden_size → 1)    # single output neuron
    """

    def __init__(
        self,
        backbone: str = "distilroberta-base",
        dropout: float = 0.3,
        hidden_size: int = 100,
    ):
        super().__init__()
        self.backbone_name = backbone
        self.encoder = AutoModel.from_pretrained(backbone)
        embed_dim = self.encoder.config.hidden_size
        self.dropout1    = nn.Dropout(dropout)
        self.hidden      = nn.Linear(embed_dim, hidden_size)
        self.relu        = nn.ReLU()
        self.dropout2    = nn.Dropout(dropout)
        self.score_head  = nn.Linear(hidden_size, 1)

    def encode_text(
        self,
        input_ids: torch.Tensor,
        attention_mask: torch.Tensor,
    ) -> torch.Tensor:
        out = self.encoder(input_ids=input_ids, attention_mask=attention_mask)
        # [CLS]-style pooling: first token (RoBERTa uses <s>)
        pooled = out.last_hidden_state[:, 0, :]
        pooled = self.dropout1(pooled)
        pooled = self.relu(self.hidden(pooled))
        pooled = self.dropout2(pooled)
        return self.score_head(pooled).squeeze(-1)

    def forward(
        self,
        input_ids_a: torch.Tensor,
        attention_mask_a: torch.Tensor,
        input_ids_b: torch.Tensor,
        attention_mask_b: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        score_a = self.encode_text(input_ids_a, attention_mask_a)
        score_b = self.encode_text(input_ids_b, attention_mask_b)
        return score_a, score_b

    def pairwise_bce_loss(
        self,
        score_a: torch.Tensor,
        score_b: torch.Tensor,
        labels: torch.Tensor,
    ) -> torch.Tensor:
        """BCE on σ(score_A - score_B) vs label (1 = A more severe)."""
        logits = score_a - score_b
        return nn.functional.binary_cross_entropy_with_logits(logits, labels.float())
