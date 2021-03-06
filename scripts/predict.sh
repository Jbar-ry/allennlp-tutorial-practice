#!/bin/bash

# (C) 2020 Dublin City University
# All rights reserved. This material may not be
# reproduced, displayed, modified or distributed without the express prior
# written permission of the copyright holder.

# Author: James Barry


test -z $1 && echo "Missing list of TBIDs (space or colon-separated)"
test -z $1 && exit 1
TBIDS=$(echo $1 | tr ':' ' ')

test -z $2 && echo "Missing package version <tagging> <tagging_stable>, whether to use bleeding-edge installation or stable release."
test -z $2 && exit 1
PACKAGE=$2

echo "using $PACKAGE"
mkdir -p output

# the following requires https://github.com/UniversalDependencies/tools to be downloaded on your system 
# and some possible perl requirements, see: https://github.com/Jbar-ry/Enhanced-UD-Parsing/issues/1 

# official shared-task data
TB_DIR=data/train-dev

UD_TOOLS_DIR=${HOME}/tools

for tbid in $TBIDS ; do
  echo
  echo "== $tbid =="
  echo

  for filepath in ${TB_DIR}/*/${tbid}-ud-train.conllu; do
    dir=`dirname $filepath`        # e.g. /home/user/ud-treebanks-v2.2/UD_Afrikaans-AfriBooms
    tb_name=`basename $dir`        # e.g. UD_Afrikaans-AfriBooms

    # ud v2.x (test data not available yet)
    GOLD=${TB_DIR}/${tb_name}/${tbid}-ud-dev.conllu
    PRED=output/${tbid}_pred.conllu
    MODEL_DIR=logs_stable/${tbid}
  
    #=== Predict ===
    allennlp predict ${MODEL_DIR}/model.tar.gz ${GOLD} \
	--output-file ${PRED} \
       	--predictor enhanced-predictor \
	--include-package "$PACKAGE" \
	--use-dataset-reader \
        --silent
  
    # collapse empty nodes in gold file
    perl ${UD_TOOLS_DIR}/enhanced_collapse_empty_nodes.pl ${GOLD} > output/${tbid}_gold_collapsed.conllu

    # collapse empty nodes in pred file
    perl ${UD_TOOLS_DIR}/enhanced_collapse_empty_nodes.pl ${PRED} > output/${tbid}_pred_collapsed.conllu

    echo "Running UD Shared Task evaluation script"
    python scripts/iwpt20_xud_eval.py --verbose output/${tbid}_gold_collapsed.conllu output/${tbid}_pred_collapsed.conllu > output/${tbid}_pred.result
  
  done
done

