#
# Author: Tiberiu Boros
#
# Copyright (c) 2019 Adobe Systems Incorporated. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import torch
import random
import torch.nn as nn
import numpy as np
from cube.networks.self_attention import SelfAttentionNetwork
from cube.networks.modules import Encoder, LinearNorm
from cube.io_utils.encodings import Encodings
from cube.io_utils.config import TaggerConfig
from cube.networks.modules import VariationalLSTM, ConvNorm


class TextEncoder(nn.Module):
    # config: TaggerConfig
    # encodings: Encodings

    def __init__(self, config, encodings, ext_conditioning=0, nn_type=VariationalLSTM, target_device='cpu'):
        super(TextEncoder, self).__init__()
        self.encodings = encodings
        self.config = config
        self.use_conditioning = (ext_conditioning == 0)
        self._target_device = target_device

        self.first_encoder = Encoder('float', self.config.tagger_embeddings_size * 2,
                                     self.config.tagger_embeddings_size * 2,
                                     self.config.tagger_encoder_size, self.config.tagger_encoder_dropout,
                                     nn_type=nn_type,
                                     num_layers=self.config.aux_softmax_layer_index, ext_conditioning=ext_conditioning)
        self.second_encoder = Encoder('float', self.config.tagger_encoder_size * 2,
                                      self.config.tagger_encoder_size * 2,
                                      self.config.tagger_encoder_size, self.config.tagger_encoder_dropout,
                                      nn_type=nn_type,
                                      num_layers=self.config.tagger_encoder_layers - self.config.aux_softmax_layer_index,
                                      ext_conditioning=ext_conditioning)
        self.character_network = SelfAttentionNetwork('float', self.config.char_input_embeddings_size,
                                                      self.config.char_input_embeddings_size,
                                                      self.config.char_encoder_size, self.config.char_encoder_layers,
                                                      self.config.tagger_embeddings_size,
                                                      self.config.tagger_encoder_dropout, nn_type=nn_type,
                                                      ext_conditioning=ext_conditioning)

        self.i2h = LinearNorm(self.config.tagger_embeddings_size * 2, self.config.tagger_encoder_size * 2)
        self.i2o = LinearNorm(self.config.tagger_embeddings_size * 2, self.config.tagger_encoder_size * 2)

        mlp_input_size = self.config.tagger_encoder_size * 2
        self.mlp = nn.Sequential(LinearNorm(mlp_input_size, self.config.tagger_mlp_layer, bias=True),
                                 nn.Tanh(),
                                 nn.Dropout(p=self.config.tagger_mlp_dropout))

        self.word_emb = nn.Embedding(len(self.encodings.word2int), self.config.tagger_embeddings_size, padding_idx=0)
        self.char_emb = nn.Embedding(len(self.encodings.char2int), self.config.char_input_embeddings_size,
                                     padding_idx=0)
        self.case_emb = nn.Embedding(4, 16,
                                     padding_idx=0)
        self.encoder_dropout = nn.Dropout(p=self.config.tagger_encoder_dropout)

        self.char_proj = LinearNorm(self.config.char_input_embeddings_size + 16, self.config.char_input_embeddings_size)

    def forward(self, x, conditioning=None):
        char_network_batch, char_network_cond_batch, word_network_batch = self._create_batches(x, conditioning)

        char_network_output = self.character_network(char_network_batch, conditioning=char_network_cond_batch)

        word_emb = self.word_emb(word_network_batch)
        char_emb = char_network_output.view(word_emb.size())
        if self.training:
            masks_char, masks_word = self._compute_masks(char_emb.size(), self.config.tagger_input_dropout_prob)
            x = torch.cat(
                (torch.tanh(masks_char.unsqueeze(2) * char_emb), torch.tanh(masks_word.unsqueeze(2) * word_emb)), dim=2)
            # x = torch.cat(
            #    (torch.tanh(0 * char_emb), torch.tanh(1 * word_emb)), dim=2)
        else:
            x = torch.cat((torch.tanh(char_emb), torch.tanh(word_emb)), dim=2)
        output_hidden, hidden = self.first_encoder(x, conditioning=conditioning)

        i2h = self.i2h(x)
        i2o = self.i2o(x)
        output_hidden = output_hidden + i2h

        # output_hidden = self.encoder_dropout(output_hidden)
        output, hidden = self.second_encoder(output_hidden, conditioning=conditioning)
        output = output + i2o
        # output = self.encoder_dropout(output)
        return self.mlp(output), output_hidden

    def _compute_masks(self, size, prob):
        m1 = np.ones(size[:-1])
        m2 = np.ones(size[:-1])

        for ii in range(m1.shape[0]):
            for jj in range(m2.shape[1]):
                p1 = random.random()
                p2 = random.random()
                if p1 >= prob and p2 < prob:
                    mm1 = 2
                    mm2 = 0
                elif p1 < prob and p2 >= prob:
                    mm1 = 0
                    mm2 = 2
                elif p1 < prob and p2 < prob:
                    mm1 = 0
                    mm2 = 0
                else:
                    mm1 = 1
                    mm2 = 1
                m1[ii, jj] = mm1
                m2[ii, jj] = mm2
        device = self._get_device()
        return torch.tensor(m1, dtype=torch.float32, device=device), torch.tensor(m2, dtype=torch.float32,
                                                                                  device=device)

    @staticmethod
    def _case_index(char):
        if char.lower() == char.upper():  # symbol
            return 3
        elif char.upper() != char:  # lowercase
            return 2
        else:  # uppercase
            return 1

    def _get_device(self):
        if self.i2h.linear_layer.weight.device.type == 'cpu':
            return 'cpu'
        return '{0}:{1}'.format(self.i2h.linear_layer.weight.device.type,
                                str(self.i2h.linear_layer.weight.device.index))

    def _create_batches(self, x, conditioning):
        char_batch = []
        char_batch_lang = []
        case_batch = []
        word_batch = []
        max_sent_size = 0
        max_word_size = 0

        for sent in x:
            if len(sent) > max_sent_size:
                max_sent_size = len(sent)
            for entry in sent:
                if len(entry.word) > max_word_size:
                    max_word_size = len(entry.word)
        # print(max_sent_size)
        for sent, cond_vec in zip(x, conditioning):
            sent_int = []

            for entry in sent:
                char_int = []
                case_int = []
                if entry.word.lower() in self.encodings.word2int:
                    sent_int.append(self.encodings.word2int[entry.word.lower()])
                else:
                    sent_int.append(self.encodings.word2int['<UNK>'])
                for char in entry.word:
                    if char.lower() in self.encodings.char2int:
                        char_int.append(self.encodings.char2int[char.lower()])
                    else:
                        char_int.append(self.encodings.char2int['<UNK>'])
                    case_int.append(self._case_index(char))
                for _ in range(max_word_size - len(entry.word)):
                    char_int.append(self.encodings.char2int['<PAD>'])
                    case_int.append(0)

                char_batch.append(char_int)
                case_batch.append(case_int)

            char_batch_lang.append(cond_vec.unsqueeze(0).repeat(max_sent_size, 1))

            for _ in range(max_sent_size - len(sent)):
                sent_int.append(self.encodings.word2int['<PAD>'])
                char_batch.append([0 for _ in range(max_word_size)])
                case_batch.append([0 for _ in range(max_word_size)])
            word_batch.append(sent_int)

        device = self._get_device()
        char_batch = self.char_emb(torch.tensor(char_batch, device=device))
        case_batch = self.case_emb(torch.tensor(case_batch, device=device))

        char_emb = torch.cat([char_batch, case_batch], dim=2)
        char_batch = self.char_proj(char_emb)
        return char_batch, torch.cat(char_batch_lang, dim=0), torch.tensor(word_batch, device=device)
