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

import optparse
import sys
import random

sys.path.append('')
import numpy as np
import torch.nn as nn
import torch.utils.data
from cube2.networks.text import TextEncoder
from cube2.config import ParserConfig
from cube.io_utils.encodings import Encodings


class Parser(nn.Module):
    encodings: Encodings
    config: ParserConfig

    def __init__(self, config, encodings, num_languages=1, target_device='cpu'):
        super(Parser, self).__init__()
        self.config = config
        self.encodings = encodings
        self.num_languages = num_languages
        self._target_device = target_device
        if num_languages == 1:
            lang_emb_size = 0
            self.lang_emb = None
        else:
            lang_emb_size = self.config.tagger_embeddings_size
            self.lang_emb = nn.Embedding(num_languages, lang_emb_size, padding_idx=0)

        self.text_network = TextEncoder(config, encodings, ext_conditioning=lang_emb_size, target_device=target_device)

        self.proj_arc = nn.Sequential(
            nn.Linear(self.config.tagger_mlp_layer + lang_emb_size, self.config.parser_arc_proj_size), nn.Tanh(),
            nn.Dropout(self.config.tagger_mlp_dropout))
        self.proj_label = nn.Sequential(
            nn.Linear(self.config.tagger_mlp_layer + lang_emb_size, self.config.parser_label_proj_size), nn.Tanh(),
            nn.Dropout(self.config.tagger_mlp_dropout))
        self.output_label = nn.Linear(self.config.parser_label_proj_size * 2, len(self.encodings.label2int))
        self.output_head = nn.Linear(self.config.parser_arc_proj_size * 2, 1)

        self.aux_mlp = nn.Sequential(
            nn.Linear(self.config.tagger_encoder_size * 2, self.config.tagger_mlp_layer),
            nn.Tanh(), nn.Dropout(p=self.config.tagger_mlp_dropout))
        self.aux_output_upos = nn.Linear(self.config.tagger_mlp_layer + lang_emb_size, len(self.encodings.upos2int))
        self.aux_output_xpos = nn.Linear(self.config.tagger_mlp_layer + lang_emb_size, len(self.encodings.xpos2int))
        self.aux_output_attrs = nn.Linear(self.config.tagger_mlp_layer + lang_emb_size, len(self.encodings.attrs2int))

    def forward(self, x, lang_ids=None):
        if lang_ids is not None and self.lang_emb is not None:
            lang_ids = torch.tensor(lang_ids, dtype=torch.long, device=self.text_network._target_device)
            lang_emb = self.lang_emb(lang_ids)
        else:
            lang_emb = None
        emb, hidden = self.text_network(x, conditioning=lang_emb)

        lang_emb = lang_emb.unsqueeze(1).repeat(1, emb.shape[1], 1)
        hidden_output = torch.cat((emb, lang_emb), dim=2)
        # from ipdb import set_trace
        # set_trace()
        proj_arc = self.proj_arc(hidden_output)
        proj_label = self.proj_label(hidden_output)
        arc_batches = []
        for batch_idx in range(emb.shape[0]):
            arc_batch = []
            for ii in range(emb.shape[1]):
                head_probs = [torch.tensor([0.0], dtype=torch.float, device=self._target_device)]
                for jj in range(emb.shape[1]):
                    w1_arc_proj = proj_arc[batch_idx, ii]
                    w2_arc_proj = proj_arc[batch_idx, jj]
                    w_arc = self.output_head(torch.cat((w1_arc_proj, w2_arc_proj), dim=0))
                    head_probs.append(w_arc)
                arc_batch.append(torch.cat(head_probs, dim=0))
            arc_batches.append(torch.stack(arc_batch))

        arcs = torch.stack(arc_batches)
        # from ipdb import set_trace
        # set_trace()

        aux_hid = self.aux_mlp(hidden)
        s_aux_upos = self.aux_output_upos(torch.cat((aux_hid, lang_emb), dim=2))
        s_aux_xpos = self.aux_output_xpos(torch.cat((aux_hid, lang_emb), dim=2))
        s_aux_attrs = self.aux_output_attrs(torch.cat((aux_hid, lang_emb), dim=2))
        return arcs, proj_label, s_aux_upos, s_aux_xpos, s_aux_attrs

    def save(self, path):
        torch.save(self.state_dict(), path)

    def load(self, path):
        self.load_state_dict(torch.load(path, map_location=self._target_device))


class TaggerDataset(torch.utils.data.Dataset):
    def __init__(self, conll_dataset):
        super(TaggerDataset, self).__init__()
        self.sequences = conll_dataset.sequences

    def __len__(self):
        return len(self.sequences)

    def __getitem__(self, item):
        return {'x': self.sequences[item][0]}


def _get_tgt_labels(data, encodings, device='cpu'):
    max_sent_size = 0
    for sent in data:
        if len(sent) > max_sent_size:
            max_sent_size = len(sent)
    tgt_upos = []
    tgt_xpos = []
    tgt_attrs = []
    tgt_arcs = []
    for sent in data:
        upos_int = []
        xpos_int = []
        attrs_int = []
        arc_int = []
        for entry in sent:
            arc_int.append(entry.head)
            if entry.upos in encodings.upos2int:
                upos_int.append(encodings.upos2int[entry.upos])
            else:
                upos_int.append(encodings.upos2int['<UNK>'])
            if entry.xpos in encodings.xpos2int:
                xpos_int.append(encodings.xpos2int[entry.xpos])
            else:
                xpos_int.append(encodings.xpos2int['<UNK>'])
            if entry.attrs in encodings.attrs2int:
                attrs_int.append(encodings.attrs2int[entry.attrs])
            else:
                attrs_int.append(encodings.attrs2int['<UNK>'])
        for _ in range(max_sent_size - len(sent)):
            upos_int.append(encodings.upos2int['<PAD>'])
            xpos_int.append(encodings.xpos2int['<PAD>'])
            attrs_int.append(encodings.attrs2int['<PAD>'])
            arc_int.append(0)
        tgt_upos.append(upos_int)
        tgt_xpos.append(xpos_int)
        tgt_attrs.append(attrs_int)
        tgt_arcs.append(arc_int)

    import torch
    return torch.tensor(tgt_arcs, device=device), torch.tensor(tgt_upos, device=device), \
           torch.tensor(tgt_xpos, device=device), torch.tensor(tgt_attrs, device=device)


def _eval(tagger, dataset, encodings, device='cpu'):
    tagger.eval()
    total = 0
    upos_ok = 0
    xpos_ok = 0
    attrs_ok = 0
    arcs_ok = 0
    num_batches = len(dataset.sequences) // params.batch_size
    if len(dataset.sequences) % params.batch_size != 0:
        num_batches += 1
    total_words = 0
    import tqdm
    pgb = tqdm.tqdm(range(num_batches), desc='\tEvaluating', ncols=80)
    tagger.eval()
    for batch_idx in pgb:
        start = batch_idx * params.batch_size
        stop = min(len(dataset.sequences), start + params.batch_size)
        data = []
        lang_ids = []
        for ii in range(stop - start):
            data.append(dataset.sequences[start + ii][0])
            total_words += len(dataset.sequences[start + ii][0])
            lang_ids.append(dataset.sequences[start + ii][1])
        with torch.no_grad():
            s_arcs, label_proj, s_upos, s_xpos, s_attrs = tagger(data, lang_ids=lang_ids)
        tgt_arcs, tgt_upos, tgt_xpos, tgt_attrs = _get_tgt_labels(data, encodings, device=device)
        s_arcs = s_upos.detach().cpu().numpy()
        s_upos = s_upos.detach().cpu().numpy()
        s_xpos = s_xpos.detach().cpu().numpy()
        s_attrs = s_attrs.detach().cpu().numpy()
        tgt_upos = tgt_upos.detach().cpu().numpy()
        tgt_xpos = tgt_xpos.detach().cpu().numpy()
        tgt_attrs = tgt_attrs.detach().cpu().numpy()
        for b_idx in range(tgt_upos.shape[0]):
            for w_idx in range(tgt_upos.shape[1]):
                pred_arc = np.argmax(s_arcs[b_idx, w_idx])
                pred_upos = np.argmax(s_upos[b_idx, w_idx])
                pred_xpos = np.argmax(s_xpos[b_idx, w_idx])
                pred_attrs = np.argmax(s_attrs[b_idx, w_idx])

                if tgt_upos[b_idx, w_idx] != 0:
                    total += 1
                    if pred_upos == tgt_upos[b_idx, w_idx]:
                        upos_ok += 1
                    if pred_xpos == tgt_xpos[b_idx, w_idx]:
                        xpos_ok += 1
                    if pred_attrs == tgt_attrs[b_idx, w_idx]:
                        attrs_ok += 1
                    if pred_arc == tgt_arcs[b_idx, w_idx]:
                        arcs_ok += 1

    return arcs_ok / total, upos_ok / total, xpos_ok / total, attrs_ok / total


def _start_train(params, trainset, devset, encodings, tagger, criterion, trainer):
    patience_left = params.patience
    epoch = 1

    best_arc = 0
    encodings.save('{0}.encodings'.format(params.store))
    tagger.config.num_languages = tagger.num_languages
    tagger.config.save('{0}.conf'.format(params.store))
    #_eval(tagger, devset, encodings, device=params.device)
    while patience_left > 0:
        patience_left -= 1
        sys.stdout.write('\n\nStarting epoch ' + str(epoch) + '\n')
        sys.stdout.flush()
        random.shuffle(trainset.sequences)
        num_batches = len(trainset.sequences) // params.batch_size
        if len(trainset.sequences) % params.batch_size != 0:
            num_batches += 1
        total_words = 0
        epoch_loss = 0
        import tqdm
        pgb = tqdm.tqdm(range(num_batches), desc='\tloss=NaN', ncols=80)
        tagger.train()
        for batch_idx in pgb:
            start = batch_idx * params.batch_size
            stop = min(len(trainset.sequences), start + params.batch_size)
            data = []
            lang_ids = []
            for ii in range(stop - start):
                data.append(trainset.sequences[start + ii][0])
                lang_ids.append(trainset.sequences[start + ii][1])
                total_words += len(trainset.sequences[start + ii][0])

            s_arcs, proj_labels, s_aux_upos, s_aux_xpos, s_aux_attrs = tagger(data, lang_ids=lang_ids)
            tgt_arc, tgt_upos, tgt_xpos, tgt_attrs = _get_tgt_labels(data, encodings, device=params.device)
            loss = (criterion(s_arcs.view(-1, s_arcs.shape[-1]), tgt_arc.view(-1)))

            loss_aux = ((criterion(s_aux_upos.view(-1, s_aux_upos.shape[-1]), tgt_upos.view(-1)) +
                         criterion(s_aux_xpos.view(-1, s_aux_xpos.shape[-1]), tgt_xpos.view(-1)) +
                         criterion(s_aux_attrs.view(-1, s_aux_attrs.shape[-1]), tgt_attrs.view(-1))) * 0.34) * \
                       tagger.config.aux_softmax_weight

            loss = loss + loss_aux
            trainer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(tagger.parameters(), 1.)
            trainer.step()
            epoch_loss += loss.item()
            pgb.set_description('\tloss={0:.4f}'.format(loss.item()))
        acc_arc, acc_upos, acc_xpos, acc_attrs = _eval(tagger, devset, encodings)
        fn = '{0}.last'.format(params.store)
        tagger.save(fn)
        if best_arc < acc_arc:
            best_arc = acc_arc
            sys.stdout.write('\tStoring {0}.bestUAS\n'.format(params.store))
            sys.stdout.flush()
            fn = '{0}.bestUPOS'.format(params.store)
            tagger.save(fn)
            patience_left = params.patience

        sys.stdout.write("\tAVG Epoch loss = {0:.6f}\n".format(epoch_loss / num_batches))
        sys.stdout.flush()
        sys.stdout.write(
            "\tValidation accuracy ARC={3:.4}, UPOS={0:.4f}, XPOS={1:.4f}, ATTRS={2:.4f}\n".format(acc_upos, acc_xpos,
                                                                                                   acc_attrs, acc_arc))
        sys.stdout.flush()
        epoch += 1


def do_debug(params):
    train_list = ['corpus/ud-treebanks-v2.4/UD_Romanian-RRT/ro_rrt-ud-train.conllu',
                  'corpus/ud-treebanks-v2.4/UD_Romanian-Nonstandard/ro_nonstandard-ud-train.conllu',
                  'corpus/ud-treebanks-v2.4/UD_French-Sequoia/fr_sequoia-ud-train.conllu',
                  'corpus/ud-treebanks-v2.4/UD_French-GSD/fr_gsd-ud-train.conllu',
                  'corpus/ud-treebanks-v2.4/UD_Portuguese-Bosque/pt_bosque-ud-train.conllu',
                  'corpus/ud-treebanks-v2.4/UD_Spanish-AnCora/es_ancora-ud-train.conllu',
                  'corpus/ud-treebanks-v2.4/UD_Catalan-AnCora/ca_ancora-ud-train.conllu',
                  'corpus/ud-treebanks-v2.4/UD_French-Spoken/fr_spoken-ud-train.conllu',
                  'corpus/ud-treebanks-v2.4/UD_Galician-CTG/gl_ctg-ud-train.conllu',
                  'corpus/ud-treebanks-v2.4/UD_Italian-ISDT/it_isdt-ud-train.conllu',
                  'corpus/ud-treebanks-v2.4/UD_Italian-PoSTWITA/it_postwita-ud-train.conllu']

    dev_list = ['corpus/ud-treebanks-v2.4/UD_Romanian-RRT/ro_rrt-ud-dev.conllu',
                'corpus/ud-treebanks-v2.4/UD_Romanian-Nonstandard/ro_nonstandard-ud-dev.conllu',
                'corpus/ud-treebanks-v2.4/UD_French-Sequoia/fr_sequoia-ud-dev.conllu',
                'corpus/ud-treebanks-v2.4/UD_French-GSD/fr_gsd-ud-dev.conllu',
                'corpus/ud-treebanks-v2.4/UD_Portuguese-Bosque/pt_bosque-ud-dev.conllu',
                'corpus/ud-treebanks-v2.4/UD_Spanish-AnCora/es_ancora-ud-dev.conllu',
                'corpus/ud-treebanks-v2.4/UD_Catalan-AnCora/ca_ancora-ud-dev.conllu',
                'corpus/ud-treebanks-v2.4/UD_French-Spoken/fr_spoken-ud-dev.conllu',
                'corpus/ud-treebanks-v2.4/UD_Galician-CTG/gl_ctg-ud-dev.conllu',
                'corpus/ud-treebanks-v2.4/UD_Italian-ISDT/it_isdt-ud-dev.conllu',
                'corpus/ud-treebanks-v2.4/UD_Italian-PoSTWITA/it_postwita-ud-dev.conllu']

    from cube.io_utils.conll import Dataset
    from cube.io_utils.encodings import Encodings
    from cube2.config import ParserConfig

    trainset = Dataset()
    devset = Dataset()
    for ii, train, dev in zip(range(len(train_list[:2])), train_list, dev_list):
        trainset.load_language(train, ii)
        devset.load_language(dev, ii)
    encodings = Encodings()
    encodings.compute(trainset, devset, word_cutoff=2)
    config = ParserConfig()
    tagger = Parser(config, encodings, len(train_list), target_device=params.device)
    if params.device != 'cpu':
        tagger.cuda(params.device)

    import torch.optim as optim
    import torch.nn as nn
    trainer = optim.Adam(tagger.parameters(), lr=2e-3, amsgrad=True, betas=(0.9, 0.9))
    criterion = nn.CrossEntropyLoss(ignore_index=0)
    if params.device != 'cpu':
        criterion.cuda(params.device)
    _start_train(params, trainset, devset, encodings, tagger, criterion, trainer)


def do_test(params):
    num_languages = 11
    from cube2.config import TaggerConfig
    from cube.io_utils.conll import Dataset
    dataset = Dataset()
    dataset.load_language(params.test_file, params.lang_id)
    encodings = Encodings()
    encodings.load(params.model_base + '.encodings')
    config = ParserConfig()
    tagger = Parser(config, encodings, num_languages, target_device=params.device)
    tagger.load(params.model_base + '.last')
    upos_acc, xpos_acc, attrs_acc = _eval(tagger, dataset, encodings, device=params.device)
    sys.stdout.write('UPOS={0}, XPOS={1}, ATTRS={2}\n'.format(upos_acc, xpos_acc, attrs_acc))


if __name__ == '__main__':
    parser = optparse.OptionParser()
    parser.add_option('--train', action='store_true', dest='train',
                      help='Start building a tagger model')
    parser.add_option('--patience', action='store', type='int', default=20, dest='patience',
                      help='Number of epochs before early stopping (default=20)')
    parser.add_option('--store', action='store', dest='store', help='Output base', default='tagger')
    parser.add_option('--batch-size', action='store', type='int', default=32, dest='batch_size',
                      help='Number of epochs before early stopping (default=32)')
    parser.add_option('--debug', action='store_true', dest='debug', help='Do some standard stuff to debug the model')
    parser.add_option('--device', action='store', dest='device', default='cpu',
                      help='What device to use for models: cpu, cuda:0, cuda:1 ...')
    parser.add_option('--test', action='store_true', dest='test', help='Test the traine model')
    parser.add_option('--test-file', action='store', dest='test_file')
    parser.add_option('--lang-id', action='store', dest='lang_id', type='int', default=0)
    parser.add_option('--model-base', action='store', dest='model_base')

    (params, _) = parser.parse_args(sys.argv)

    if params.debug:
        do_debug(params)
    if params.test:
        do_test(params)
