#!/usr/bin/env python3
"""
Step 1 of the ORCA scan demo, run on your own machine: write one ORCA
input per point of a 5 x 5 scan of one atom's position, plus the
`scan_info.json` manifest the cluster side reads its task list from.

Needs McUtils and Psience (they are not hpclib dependencies):

    python generate_scan.py ~/Desktop/sample_scan

The core count and per-core memory written into the inputs must match
what the `orca` template asks SLURM for (`nprocs`, `mem`); see README.md.
"""
import argparse
import os

from Psience.Molecools import Molecule
from Psience.Data import ScanManager, molecule_atom_position_scan_iterator

# methyl vinyl ketone enol tautomer, as in Psience's MolecoolsTests
SMILES = (
    'C(C(=O)C(=C(O[H])C([H])([H])[H])[H])([H])([H])[H]'
    '_qT8ZMGmthj2Aqc40gKmYOACowCH/pC8/f6ntJmiZeSa+l80nFZ7uJQCqmSZonNYnvZUqJxWc'
)


def atom_frame(mol, atom):
    """
    The local axis frame the scanned atom moves in. Current Psience has this
    as `MoleculeBuilder.fragment_embedding`, while `ScanManager` looks for a
    `Molecule.fragment_embedding` method, so compute it here and pass it in.
    """
    if hasattr(mol, "fragment_embedding"):
        return mol.fragment_embedding([atom], return_axes=True)[2]
    from Psience.Molecools import MoleculeBuilder
    return MoleculeBuilder.fragment_embedding(mol, [atom], return_axes=True)[2]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("output_directory", help="where the scan's .inp files and scan_info.json go")
    parser.add_argument("--atom", type=int, default=6, help="index of the atom to move (default %(default)s)")
    parser.add_argument("--points", type=int, default=5, help="grid points per axis (default %(default)s)")
    parser.add_argument("--extent", type=float, default=1.0,
                        help="displacement range, -extent..extent along each axis (default %(default)s)")
    parser.add_argument("--nprocs", type=int, default=4, help="cores per calculation (default %(default)s)")
    parser.add_argument("--maxcore", type=int, default=3500, help="ORCA MaxCore, MB per core (default %(default)s)")
    parser.add_argument("--level-of-theory", default="B97-3c")
    opts = parser.parse_args(argv)

    mol = Molecule.from_string(SMILES, 'smi').get_embedded_molecule()
    manager = ScanManager(os.path.expanduser(opts.output_directory))
    _, scan_iterator = molecule_atom_position_scan_iterator(
        mol,
        [opts.atom],
        [
            [-opts.extent, opts.extent, opts.points],
            [-opts.extent, opts.extent, opts.points],
        ],
        embedding=atom_frame(mol, opts.atom),
        return_values=True,
        zigzag=True
    )
    scan_dir, info, steps = manager.generate(
        scan_iterator,
        job_type='orca',
        commands=['opt'],
        level_of_theory=opts.level_of_theory,
        nproc=opts.nprocs,      # -> %pal nprocs; the orca template checks this against SLURM
        memory=opts.maxcore,    # -> %MaxCore; keep nprocs * maxcore under the job's `mem`
        overwrite=True
    )
    print(f"wrote {len(steps)} inputs and {info['file']}")


if __name__ == "__main__":
    main()
