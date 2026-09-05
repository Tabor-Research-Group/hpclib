#! /usr/bin/env python

"""Installer script."""
import setuptools

with open("README.md") as f:
    long_description = f.read()

setuptools.setup(
    name="hpclib",
    version="0.0.1",
    description="Convenient HPC toolkits",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/Tabor-Research-Group/hpclib",
    author="",
    author_email="",
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: Apache Software License",
        "Operating System :: OS Independent",
    ],
    packages=setuptools.find_packages(),
    include_package_data=True,
    python_requires=">=3.10",
    install_requires=[
        # "ase",
        # "pysisyphus",
        # "rdkit",
        # "matplotlib",
        # "mccoygroup-mcutils",
        # "mccoygroup-psience"
    ]
)
