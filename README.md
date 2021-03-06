# RGN2

This is the reference implementation of recurrent geometric network-2 (RGN2), described in the paper [Single-sequence protein structure prediction using a language model and deep learning]

* Python 3
* TensorFlow 1.14
* setproctitle

### Note: Only use RGN2 if you are predicting a structure of a helical orphan protein. For proteins with known homologs please use AlphaFold2 or OpenFold Colab notebooks.
#### AlphaFold2 Colab:https://colab.research.google.com/github/sokrypton/ColabFold/blob/main/AlphaFold2.ipynb
#### OpenFold Colab: https://colab.research.google.com/github/aqlaboratory/openfold/blob/main/notebooks/OpenFold.ipynb

## Usage
Open the rgn2_prediction.ipynb and paste your helical orphan protein sequence and follow the steps therein to predict the 3D structure.


## Pre-print Reference
[Single-sequence protein structure prediction using language models from deep learning, biorXiv 2021](https://www.biorxiv.org/content/10.1101/2021.08.02.454840v1)
