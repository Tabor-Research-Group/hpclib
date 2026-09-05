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
    package_data={
        "hpclib": [
            "*.sh",  # hpclib/hpclib.sh itself
            "lib/*.sh",  # hpclib/lib/core.sh, connections.sh, slurm.sh, tunnels.sh, applications.sh, job_queue.sh
            "templates/*.sh",  # hpclib/templates/sbatch_core.sh
            "tunnels/*.sh",  # hpclib/tunnels/start_tunnel.sh, configure_job.sh, postconnect.sh
            "tunnels/*/*.sh",  # hpclib/tunnels/{jupyter,ngl,pai,vscode}/*.sh
            "tunnels/*/*.py",  # hpclib/tunnels/ngl/mdsrv_start.py - not a package, so this is the ONLY
            # way it gets installed at all
        ],
        "hpclib.job_queue": [
            "templates/*.sh",  # hpclib/job_queue/templates/*.sh - same non-package-subdir situation
        ],
    }
)
