import numpy as np
import redshift_utils   as ru
import quasar_fit_basis as qfb

###
### Experiment Params
###
SPLIT_TYPE        = "redshift" #split_types = ["random", "flux", "redshift"]
NUM_TRAIN_EXAMPLE = 2000
SEED              = 42
BASIS_DIR         = "cache/basis_fits/"

if __name__=="__main__":

    print \
""" 
=============== CACHING QSO MATRICES  ====================
  split type = {split}
  num train  = {num_train}
  seed       = {seed}
  saving to  = {odir}
==========================================================
""".format(split     = SPLIT_TYPE,
           num_train = NUM_TRAIN_EXAMPLE,
           seed      = SEED,
           odir      = BASIS_DIR)

    # DR10 qso dataset and spec files
    qso_psf_flux, qso_psf_mags, qso_z, spec_files, train_idx, test_idx = \
        ru.load_DR10QSO_train_test_idx(split_type = SPLIT_TYPE)

    ## randomly subselect NUM_TRAIN
    np.random.seed(SEED)
    rand_idx      = np.random.permutation(len(train_idx))
    train_idx_sub = train_idx[rand_idx[0:NUM_TRAIN_EXAMPLE]]

    ## only load in NUM_TRAIN spec files
    train_spec_files = np.array(spec_files)[train_idx_sub]
    spec_grid, spec_ivar_grid, spec_mod_grid, unique_lams, spec_zs, spec_ids, badids = \
        ru.load_specs_from_disk(train_spec_files)

    ## cache 
    CACHE_TRAIN_FILE = qfb.cache_file_name(SPLIT_TYPE, NUM_TRAIN_EXAMPLE)
    with open(CACHE_TRAIN_FILE, 'wb') as handle:
        np.save(handle, train_idx)
        np.save(handle, spec_grid)
        np.save(handle, spec_ivar_grid)
        np.save(handle, spec_mod_grid)
        np.save(handle, unique_lams)
        np.save(handle, spec_zs)
        np.save(handle, spec_ids)

