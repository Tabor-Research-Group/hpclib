#! /usr/bin/env python

"""Installer script."""
import re
import setuptools

# single source of truth for the version: hpclib/hpclib.sh
with open("hpclib/hpclib.sh") as f:
    version = re.search(r'^HPCLIB_VERSION="([^"]+)"', f.read(), re.M).group(1)

with open("README.md") as f:
    long_description = f.read()

setuptools.setup(
    name="hpclib",
    version=version,
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
    # only hpclib/servers/rest_mcp.py (run on your own machine) needs this
    extras_require={"mcp": ["mcp>=1.10"]},
    package_data={
        "hpclib": [
            "*.sh",  # hpclib/hpclib.sh itself
            "lib/*.sh",  # hpclib/lib/core.sh, connections.sh, slurm.sh, tunnels.sh, applications.sh, job_queue.sh
            "templates/*.sh",  # hpclib/templates/sbatch_core.sh
            "tunnels/*.sh",  # hpclib/tunnels/start_tunnel.sh, configure_job.sh, postconnect.sh
            "tunnels/*/*.sh",  # hpclib/tunnels/{jupyter,ngl,pai,vscode}/*.sh
            "tunnels/*/*.py",  # hpclib/tunnels/ngl/mdsrv_start.py - not a package, so this is the ONLY
            # way it gets installed at all
            "tunnels/rest/templates/*/*",  # example REST job templates (template.json + script.sh)
            "tunnels/rest/templates/*/*/*",  # ... and their examples/*.json
            "examples/*/*",  # worked examples, e.g. examples/orca_scan
        ],
        "hpclib.job_queue": [
            "templates/*.sh",  # hpclib/job_queue/templates/*.sh - same non-package-subdir situation
        ],
    }
)
