#!/usr/bin/env python
# -*- coding: utf-8 -*-

# (C) 2020 Dublin City University
# All rights reserved. This material may not be
# reproduced, displayed, modified or distributed without the express prior
# written permission of the copyright holder.

# Author: Joachim Wagner

# For Python 2-3 compatible code
# https://python-future.org/compatible_idioms.html

from __future__ import print_function

import os
import subprocess
import sys
import time

def print_usage():
    print('Usage: %s [--evaldir] FOLDER' %sys.argv[0])
    
example_entries = """
udpstd_nl_alpino-e7_elmo_udpf_task_nl_alpino+task_nl_lassysmall-copy2e-for-nl_lassysmall-ud-dev.eval.txt
udpstd_fi_tdt-e7_fasttext_udpf_task_fi_tdt-allennlp_090_dm_lbert_u_fi_tdt_20200420_050020-for-fi_tdt-ud-dev.eval.txt
udpstd_pl_lfg-e7_elmo_udpf_task_pl_lfg+task_pl_pdb-allennlp_090_dm_mbert_u_pl_lfg_20200420_031928-for-pl_pdb-ud-dev.eval.txt
udpstd_lv_lvtb-e3_elmo_udpf_task_lv_lvtb_plain_udpf_task_lv_lvtb-copy2e-for-lv_lvtb-ud-dev.eval.txt
"""

def main():
    opt_eval_dir = 'data/predictions'
    opt_help = False
    if len(sys.argv) in (2,3):
        option = sys.argv[1]
        if option[:3] in ('-h', '-he', '--h'):
            opt_help = True
        elif option in ('--evaldir', '--eval-dir', '--eval_dir'):
            opt_eval_dir = sys.argv[2]
        elif len(sys.argv) == 2:
            opt_eval_dir = option
        else:
            opt_help = True
    elif len(sys.argv) > 1:
        opt_help = True
    if opt_help:
        print_usage()
        sys.exit()
    tbids = set()
    metrics = set()
    parts = set()
    data = {}
    for filename in os.listdir(opt_eval_dir):
        if filename.endswith('-ud-dev.eval.txt'):
            full_path = '/'.join((opt_eval_dir, filename))
            if os.path.getsize(full_path) == 0:
                continue
            fields = filename.split('-')
            if len(fields) != 7:
                raise ValueError('wrong number of fields in %s: %r' %(filename, fields))
            segmenter = fields[0]
            basic_parser = fields[1]
            enhancer = fields[2]
            system = (segmenter, basic_parser, enhancer)
            assert fields[3] == 'for'
            tbid = fields[4]
            ptime = os.path.getmtime(full_path[:-9]+'.conllu')
            f = open(full_path, 'rb')
            example_contents = """
Metric     | Precision |    Recall |  F1 Score | AligndAcc
-----------+-----------+-----------+-----------+-----------
Tokens     |     99.99 |     99.98 |     99.99 |
Sentences  |     98.58 |     99.34 |     98.96 |
Words      |     95.79 |     93.26 |     94.51 |
UPOS       |     93.62 |     91.15 |     92.37 |     97.74
XPOS       |     90.61 |     88.21 |     89.39 |     94.59
UFeats     |     90.73 |     88.33 |     89.52 |     94.72
AllTags    |     90.39 |     88.00 |     89.18 |     94.37
Lemmas     |     91.36 |     88.94 |     90.13 |     95.37
UAS        |     79.83 |     77.72 |     78.76 |     83.34
LAS        |     76.22 |     74.21 |     75.20 |     79.57
ELAS       |     72.64 |     68.47 |     70.50 |     78.07
EULAS      |     74.51 |     70.24 |     72.31 |     80.08
CLAS       |     72.50 |     72.27 |     72.38 |     76.66
MLAS       |     68.15 |     67.93 |     68.04 |     72.06
BLEX       |     68.86 |     68.65 |     68.76 |     72.81
"""
            assert f.readline().startswith('Metric')
            assert f.readline().startswith('------')
            while True:
                line = f.readline()
                if not line:
                    break
                fields = line.replace('|', ' ').split()
                assert len(fields) in (4,5)
                metric = fields[0]
                for part, index in [
                    ('P', 1),
                    ('R', 2),
                    ('F1', 3),
                    ('AA', 4),
                ]:
                    table_key = (tbid, metric, part)
                    try:
                        score_as_str = fields[index]
                    except IndexError:
                        continue
                    tbids.add(tbid)
                    metrics.add(metric)
                    parts.add(part)
                    if not table_key in data:
                        data[table_key] = []
                    negscore = -float(score_as_str)
                    data[table_key].append((negscore, score_as_str, system, ptime))
            f.close()
    # all data ready
    for tbid in tbids:
        if tbid == 'et_ewt':   # exclude as we added fake dev data
            continue
        for metric in metrics:
            for part in parts:
                table_key = (tbid, metric, part)
                try:
                    table = data[table_key]
                except KeyError:
                    continue
                f = open('%s_%s_%s.html' %table_key, 'wb')
                f.write('<html><head><title>%s %s %s</title></head>\n' %table_key)
                f.write('<body>\n')
                # navigation
                f.write('treebanks: ')
                for nav_tbid in sorted(list(tbids)):
                    if nav_tbid == 'et_ewt':
                        continue
                    if nav_tbid != tbid:
                        f.write(' - <a href="%s_%s_%s.html">' %(nav_tbid, metric, part))
                    f.write(nav_tbid)
                    if nav_tbid != tbid:
                        f.write('</a> - ')
                f.write('</br>\n')
                f.write('metrics: ')
                for nav_metric in sorted(list(metrics)):
                    if nav_metric != metric:
                        f.write(' - <a href="%s_%s_%s.html">' %(tbid, nav_metric, part))
                    f.write(nav_metric)
                    if nav_metric != metric:
                        f.write('</a> - \n')
                f.write('</br>\n')
                f.write('parts: ')
                for nav_part in sorted(list(parts)):
                    if nav_part != part:
                        f.write(' - <a href="%s_%s_%s.html">' %(tbid, metric, nav_part))
                    f.write(nav_part)
                    if nav_part != part:
                        f.write('</a> - \n')
                f.write('</br>\n')
                # content
                f.write('<h1>%s %s %s</h1>\n' %table_key)
                f.write('<table border="1" cellpadding="4">\n')
                f.write('<tr><th>Segmenter</th><th>Basic Parser</th><th>Enhancer</th><th>Score</th><th>Predicted</th></tr>\n')
                table.sort()
                for _, score_as_string, system, ptime in table:
                    f.write('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>\n' %(
                        system[0].replace('_', ' '),
                        system[1].replace('_', ' ').replace('+', ' + '),
                        system[2].replace('_', ' '),
                        score_as_string,
                        time.ctime(ptime),
                    ))
                f.write('</table>\n')
                f.write('</body>\n')
                f.write('</html>\n')
                f.close()

main()
