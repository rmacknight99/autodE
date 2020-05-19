from copy import deepcopy
import numpy as np
from autode.solvent.qmmm import get_species_point_charges
from autode.atoms import get_atomic_weight
from autode.calculation import Calculation
from autode.config import Config
from autode.exceptions import AtomsNotFound
from autode.exceptions import NoCalculationOutput
from autode.exceptions import NoNormalModesFound
from autode.geom import length
from autode.log import logger
from autode.methods import get_hmethod, get_lmethod
from autode.molecule import Molecule
from autode.mol_graphs import make_graph
from autode.mol_graphs import species_are_isomorphic
from autode.species import Species


class TSbase(Species):

    def _init_graph(self):
        """Set the molecular graph for this TS object from the reactant"""
        logger.warning(f'Setting the graph of {self.name} from reactants')

        self.graph = self.reactant.graph.copy()
        return None

    def could_have_correct_imag_mode(self, method=None, threshold=-50):
        """
        Determine if a point on the PES could have the correct imaginary mode. This must have

        (0) An imaginary frequency      (quoted as negative in most EST codes)
        (1) The most negative(/imaginary) is more negative that a threshold

        Keywords Arguments:
            method (autode.wrappers.base.ElectronicStructureMethod):
            threshold (float):
        """

        # By default the high level method is used to check imaginary modes
        if method is None:
            method = get_hmethod()

        if self.calc is None:
            logger.info('Calculating the hessian..')
            self.calc = Calculation(name=self.name + '_hess', molecule=self, method=method,
                                    keywords_list=method.keywords.hess, n_cores=Config.n_cores,
                                    point_charges=get_species_point_charges(self))
            self.calc.run()

        imag_freqs = self.calc.get_imag_freqs()

        if len(imag_freqs) == 0:
            logger.warning('Hessian had no imaginary modes')
            return False

        if len(imag_freqs) > 1:
            logger.warning(f'Hessian had {len(imag_freqs)} imaginary modes')

        if imag_freqs[0] > threshold:
            logger.warning('Imaginary modes were too small to be significant')
            return False

        if not ts_has_contribution_from_active_atoms(calc=self.calc, active_atoms=self.bond_rearrangement.active_atoms):
            logger.warning('Species does not have the correct imaginary mode')
            return False

        logger.info('Species could have the correct imaginary mode')
        return True

    def has_correct_imag_mode(self, calc=None, method=None):
        """Check that the imaginary mode is 'correct' set the calculation (hessian or optts)"""
        self.calc = calc if calc is not None else self.calc

        # By default the high level method is used to check imaginary modes
        if method is None:
            method = get_hmethod()

        # Run a fast check on  whether it's likely the mode is correct
        if not self.could_have_correct_imag_mode(method=method):
            return False

        if imag_mode_has_correct_displacement(calc=self.calc, bond_rearrangement=self.bond_rearrangement):
            logger.info('Displacement of the active atoms in the imaginary mode bond forms and breaks the correct bonds')
            return True

        # Perform displacements over the imaginary mode to ensure the mode connects reactants and products
        if imag_mode_links_reactant_products(self.calc, self.reactant, self.product, method=method):
            logger.info('Imaginary mode does link reactants and products')
            return True

        logger.warning('Species does *not* have the correct imaginary mode')
        return False

    def __init__(self, atoms, reactant, product, name='ts_guess'):

        super().__init__(name=name, atoms=atoms, charge=reactant.charge, mult=reactant.mult)
        self.solvent = reactant.solvent
        self.atoms = atoms

        self.reactant = reactant
        self.product = product

        self.calc = None
        self.bond_rearrangement = None

        self._init_graph()


def ts_has_contribution_from_active_atoms(calc, active_atoms, threshold=0.1):
    """For a hessian calculation check that the first imaginary mode (number 6) in the final frequency calculation
    contains the correct motion, i.e. contributes more than threshold_contribution in relative terms to the
    magnitude of the sum of the forces

    Arguments:
        calc (autode.calculation.Calculation): calculation object
        active_atoms (list(int)):

    Keyword Arguments:
        threshold (float): threshold contribution to the imaginary mode from the atoms in

    Returns:
        (bool): if the imaginary mode is correct or not
    """
    logger.info(f'Checking the active atoms contribute more than {threshold} to the imag mode')

    try:
        imag_normal_mode_displacements_xyz = calc.get_normal_mode_displacements(mode_number=6)

    except (NoNormalModesFound, NoCalculationOutput):
        logger.error('Have no imaginary normal mode displacements to analyse')
        return False

    # If there are normal modes then there should be atoms in the output..
    atoms = calc.get_final_atoms()

    # Calculate the magnitudes of the motion on each atom weighted by the atomic weight
    imag_mode_magnitudes = [length(np.array(dis_xyz)) for dis_xyz in imag_normal_mode_displacements_xyz]
    weighted_imag_mode_magnitudes = [get_atomic_weight(atom_label=atoms[i].label) + 10 * imag_mode_magnitudes[i]
                                     for i in range(len(atoms))]

    # Calculate the sum of the weighted magnitudes on the active atoms
    sum_active_atom_magnitudes = sum([weighted_imag_mode_magnitudes[atom_index] for atom_index in active_atoms])

    rel_contribution = sum_active_atom_magnitudes / np.sum(np.array(weighted_imag_mode_magnitudes))

    if rel_contribution > threshold:
        logger.info(f'Significant contribution from active atoms to imag mode. (contribution = {rel_contribution:.3f})')
        return True

    else:
        logger.warning(f'TS has *no* significant contribution from the active atoms to the imag mode '
                       f'(contribution = {rel_contribution:.3f})')
        return False


def get_displaced_atoms_along_mode(calc, mode_number, disp_magnitude=1.0):
    """Displace the geometry along the imaginary mode with mode number iterating from 0, where 0-2 are translational
    normal modes, 3-5 are rotational modes and 6 is the largest imaginary mode. To displace along the second imaginary
    mode we have mode_number=7

    Arguments:
        calc (autode.calculation.Calculation):
        mode_number (int): Mode number to displace along

    Keyword Arguments:
        disp_magnitude (float): Distance to displace (default: {1.0})

    Returns:
        (list(autode.atoms.Atom)):
    """
    logger.info('Displacing along imaginary mode')

    atoms = deepcopy(calc.get_final_atoms())
    mode_disp_coords = calc.get_normal_mode_displacements(mode_number=mode_number)      # n x 3 array

    assert len(atoms) == len(mode_disp_coords)

    for i in range(len(atoms)):
        atoms[i].translate(vec=disp_magnitude * mode_disp_coords[i, :])

    return atoms


def imag_mode_has_correct_displacement(calc, bond_rearrangement, disp_mag=1.0, delta_threshold=0.3):
    """
    Check whether the imaginary mode in a calculation with a hessian forms and breaks the correct bonds

    Arguments:
        calc (autode.calculation.Calculation):
        bond_rearrangement (autode.bond_rearrangement.BondRearrangement):

    Keyword Arguments:
        disp_mag (float):
        delta_threshold (float): Required ∆r on a bond for the bond to be considered as forming
    """
    logger.info('Checking displacement on imaginary mode forms the correct bonds')
    ts_species = deepcopy(calc.molecule)

    f_displaced_atoms = get_displaced_atoms_along_mode(calc, mode_number=6, disp_magnitude=disp_mag)
    f_species = Species(name='f_displaced', atoms=f_displaced_atoms, charge=0, mult=1)  # Charge & mult are placeholders

    b_displaced_atoms = get_displaced_atoms_along_mode(calc, mode_number=6, disp_magnitude=-disp_mag)
    b_species = Species(name='b_displaced', atoms=b_displaced_atoms, charge=0, mult=1)

    if imag_mode_generates_other_bonds(ts_species, f_species, b_species, bond_rearrangement):
        logger.warning('Imaginary mode generates bonds that are not active..')
        return False

    # Product could be either the forward displaced molecule or the backwards equivalent
    for product in (f_species, b_species):

        fbond_bbond_correct_disps = []

        for fbond in bond_rearrangement.fbonds:

            ts_dist = ts_species.get_distance(*fbond)
            p_dist = product.get_distance(*fbond)

            # Displaced distance towards products should be shorter than the distance at the TS if the bond is forming
            if ts_dist - p_dist > delta_threshold:
                fbond_bbond_correct_disps.append(True)

            else:
                fbond_bbond_correct_disps.append(False)

        for bbond in bond_rearrangement.bbonds:

            ts_dist = ts_species.get_distance(*bbond)
            p_dist = product.get_distance(*bbond)

            # Displaced distance towards products should be longer than the distance at the TS if the bond is breaking
            if p_dist - ts_dist > delta_threshold:
                fbond_bbond_correct_disps.append(True)

            else:
                fbond_bbond_correct_disps.append(False)

        logger.info(f'List of forming and breaking bonds that have the correct properties {fbond_bbond_correct_disps}')
        if all(fbond_bbond_correct_disps):
            logger.info(f'{product.name} afforded the correct bond forming/breaking reactants -> products')
            return True

    logger.warning('Displacement along the imaginary mode did not form and break the correct bonds')
    return False


def imag_mode_generates_other_bonds(ts, f_species, b_species, bond_rearrangement):
    """Determine if the forward or backwards displaced molecule break or make bonds that aren't in all the active bonds
    bond_rearrangement.all. Will be fairly conservative here"""

    for species in (ts, f_species, b_species):
        make_graph(species, rel_tolerance=0.3)

    for product in (f_species, b_species):

        new_bonds_in_product = set([bond for bond in product.graph.edges if bond not in ts.graph.edges])

        # If there are new bonds in the forward displaced species that are not part of the bond rearrangement
        if any(bond not in bond_rearrangement.all for bond in new_bonds_in_product):
            logger.warning(f'New bonds in product: {new_bonds_in_product}')
            logger.warning(f'Bond rearrangement: {bond_rearrangement.all}')
            return True

    logger.info('Imaginary mode does generate any other unwanted bonds')
    return False


def imag_mode_links_reactant_products(calc, reactant, product, method, disp_mag=1.0):
    """Displaces atoms along the imaginary mode forwards (f) and backwards (b) to see if products and reactants are made

    Arguments:
        calc (autode.calculation.Calculation):
        reactant (autode.complex.ReactantComplex):
        product (autode.complex.ProductComplex):
        method (autode.wrappers.base.ElectronicStructureMethod):

    Keyword Arguments:
        disp_mag (int): Distance to be displaced along the imag mode (default: {1})

    Returns:
        bool: if the imag mode is correct or not
    """
    logger.info('Displacing along imag modes to check that the TS links reactants and products')

    # Generate and optimise conformers with the low level of theory
    reactant.populate_conformers()
    product.populate_conformers()

    # Get the species that is optimised by displacing forwards along the imaginary mode
    f_displaced_atoms = get_displaced_atoms_along_mode(calc, mode_number=6, disp_magnitude=disp_mag)
    f_displaced_mol = get_optimised_species(calc, method, direction='forwards', atoms=f_displaced_atoms)

    # Get the species that is optimised by displacing backwards along the imaginary mode
    b_displaced_atoms = get_displaced_atoms_along_mode(calc, mode_number=6, disp_magnitude=-disp_mag)
    b_displaced_mol = get_optimised_species(calc, method, direction='backwards', atoms=b_displaced_atoms)

    if forwards_backwards_are_isomorphic_to_reactants_and_products(f_displaced_mol, b_displaced_mol, reactant, product):
        return True

    # The high and low level methods may not have the same minima, so optimise and recheck isomorphisms
    for mol in (f_displaced_mol, b_displaced_mol):
        mol.optimise(method=get_lmethod(), reset_graph=True)

    if forwards_backwards_are_isomorphic_to_reactants_and_products(f_displaced_mol, b_displaced_mol, reactant, product):
        return True

    logger.info(f'Forwards displaced edges {f_displaced_mol.graph.edges}')
    logger.info(f'Backwards displaced edges {b_displaced_mol.graph.edges}')
    return False


def forwards_backwards_are_isomorphic_to_reactants_and_products(forwards, backwards, reactant, product):

    if any(mol.atoms is None for mol in (forwards, backwards)):
        logger.warning('Atoms not set in the output. Cannot calculate isomorphisms')
        return False

    if species_are_isomorphic(backwards, reactant) and species_are_isomorphic(forwards, product):
        logger.info('Forwards displacement lead to products and backwards reactants')
        return True

    if species_are_isomorphic(forwards, reactant) and species_are_isomorphic(backwards, product):
        logger.info('Backwards displacement lead to products and forwards to reactants')
        return True

    return False


def get_optimised_species(calc, method, direction, atoms):
    """Get the species that is optimised from an initial set of atoms"""

    species = Molecule(name=f'{calc.name}_{direction}', atoms=atoms, charge=calc.molecule.charge, mult=calc.molecule.mult)

    # Note that for the surface to be the same the keywords.opt and keywords.hess need to match in the level of theory
    calc = Calculation(name=f'{calc.name}_{direction}', molecule=species, method=method,
                       keywords_list=method.keywords.opt, n_cores=Config.n_cores, opt=True)
    calc.run()

    try:
        species.set_atoms(atoms=calc.get_final_atoms())
        species.energy = calc.get_energy()
        make_graph(species)

    except AtomsNotFound:
        logger.error(f'{direction} displacement calculation failed')

    return species
