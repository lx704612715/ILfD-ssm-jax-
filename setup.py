#!/usr/bin/env python
from distutils.core import setup
import setuptools
import os


setup(
    name="ssm",
    version="0.1",
    description="Bayesian learning and inference for a variety of state space models",
    author="Scott Linderman",
    author_email="scott.linderman@stanford.edu",
    url="https://github.com/lindermanlab/ssm",
    install_requires=[
        "numpy",
        "scipy==1.9.3",
        "matplotlib",
        "scikit-learn==1.1.3",
        "tqdm",
        "seaborn",
        "jax==0.3.25",
        "jaxlib==0.3.25",
        "jupyter",
        "ipywidgets",
        "tensorflow-probability==0.17.0",
	"flax",
 	"optax",
    ],
    packages=setuptools.find_packages(),
)
