#!/bin/bash

# (C) 2020 Dublin City University
# All rights reserved. This material may not be
# reproduced, displayed, modified or distributed without the express prior
# written permission of the copyright holder.

# Author: James Barry


test -z $1 && echo "Missing task type <basic> or <enhanced>"
test -z $1 && exit 1
TASK=$1

test -z $2 && echo "Missing model type <dm> or <kg>"
test -z $2 && exit 1
MODEL_TYPE=$2

test -z $3 && echo "Missing list of TBIDs (space or colon-separated)"
test -z $3 && exit 1
TBIDS=$(echo $3 | tr ':' ' ')

test -z $4 && echo "Missing random seed"
test -z $4 && exit 1
RANDOM_SEED=$4


test -z $5 && echo "Missing package version <tagging> <tagging_stable>"
test -z $5 && exit 1
PACKAGE=$5

# official shared-task data (filtered contains treebanks with long sentences removed.)
TB_DIR=data/train-dev

TIMESTAMP=`date "+%Y%m%d-%H%M%S"` 

N_SHORT=`echo ${HOSTNAME} | cut -c-5 `
if [ "${N_SHORT}" = "node0" ]; then
  echo "loading CUDA"
  module add cuda10.1
fi


for tbid in $TBIDS ; do
  echo
  echo "== $tbid =="
  echo

  # seed
  SEED=$RANDOM_SEED
  PYTORCH_SEED=`expr $SEED / 10`
  NUMPY_SEED=`expr $PYTORCH_SEED / 10`
  export RANDOM_SEED=$SEED
  export PYTORCH_SEED=$PYTORCH_SEED
  export NUMPY_SEED=$NUMPY_SEED

  # hyperparams
  export BATCH_SIZE=8
  export NUM_EPOCHS=50
  export CUDA_DEVICE=0

  for filepath in ${TB_DIR}/*/${tbid}-ud-train.conllu; do
    dir=`dirname $filepath`        # e.g. /home/user/ud-treebanks-v2.2/UD_Afrikaans-AfriBooms
    tb_name=`basename $dir`        # e.g. UD_Afrikaans-AfriBooms

    # ud v2.x
    export TRAIN_DATA_PATH=${TB_DIR}/${tb_name}/${tbid}-ud-train.conllu
    export DEV_DATA_PATH=${TB_DIR}/${tb_name}/${tbid}-ud-dev.conllu
    export TEST_DATA_PATH=${TB_DIR}/${tb_name}/${tbid}-ud-test.conllu
          
    if [ "${PACKAGE}" = "tagging_stable" ]; then
    allennlp train configs/stable/ud_enhanced_dm_u.jsonnet -s logs/${tbid}-${TASK}-${MODEL_TYPE}-seed-${RANDOM_SEED}-${TIMESTAMP} --include-package ${PACKAGE}
    elif [ "${PACKAGE}" = "tagging" ]; then
    allennlp train configs/ud_${TASK}_${MODEL_TYPE}_luxfb.jsonnet -s logs/${tbid}-${TASK}-${MODEL_TYPE}-seed-${RANDOM_SEED}-${TIMESTAMP} --include-package ${PACKAGE}
    fi
  done
done

