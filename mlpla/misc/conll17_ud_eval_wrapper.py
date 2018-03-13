#
# Author: Tiberiu Boros
#
# Copyright (c) 2018 Adobe Systems Incorporated. All rights reserved.
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

from conll17_ud_eval import load_conllu_file, evaluate

#metrics = ["Tokens", "Sentences", "Words", "UPOS", "XPOS", "Feats", "AllTags", "Lemmas", "UAS", "LAS"]
#example usage:     metrics_test = conll_eval(system,gold)
#                   test_tok_f1, test_ss_f1 = metrics_test["Tokens"].f1*100., metrics_test["Sentences"].f1*100.
def conll_eval(system_file, gold_file):    
    gold_ud = load_conllu_file(gold_file)#"/work/work/taa/corpus/ud-test-v2.0-conll2017/gold/conll17-ud-test-2017-05-09/ro.conllu")
    system_ud = load_conllu_file(system_file)#"/work/work/sq2/sequence_labeler/ro.conllu")        
    return evaluate(gold_ud, system_ud, None)   
    