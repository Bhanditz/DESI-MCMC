import sys, os
from glob import glob
import fitsio
import cPickle as pickle
import numpy as np
import numpy.random as npr
from CelestePy.util.misc import check_grad
from CelestePy.util.infer.optimizers import *
from scipy.optimize import minimize
sys.path.append('../../')
import redshift_utils   as ru
import quasar_fit_basis as qfb
import GPy

###
### Experiment Params
###
SPLIT_TYPE        = "redshift"  #split_types = ["random", "flux", "redshift"]
NUM_TRAIN_EXAMPLE = 2000
MAX_LBFGS_ITER    = 10000
NUM_BASES         = 4
BETA_VARIANCE     = 1.
BETA_LENGTHSCALE  = 40.
BASIS_DIR         = "cache/basis_locked/"

# set up experiment list
import itertools
Ks         = [3, 6, 8, 16, 32]
SPLITS     = ["random", "flux", "redshift"]
EXP_PARAMS = list(itertools.product(Ks, SPLITS))
for i,ep in enumerate(EXP_PARAMS):
    print "task_no %d: "%i, ep

def initialize_from_lower_res(th_lo, lam_lo, parser_lo, 
                                     lam_hi, parser_hi ):
    """ initialize params from lower resolution """

    betas_lo  = parser_lo.get(th_lo, 'betas')
    omegas_lo = parser_lo.get(th_lo, 'omegas')
    mus_lo    = parser_lo.get(th_lo, 'mus')

    # linearly interpolate
    th_hi = np.zeros(parser_hi.N)
    betas_hi = np.array([np.interp(lam_hi, lam_lo, beta) for beta in betas_lo])
    parser_hi.set(th_hi, 'betas', betas_hi)
    parser_hi.set(th_hi, 'omegas', omegas_lo)
    parser_hi.set(th_hi, 'mus', mus_lo)
    return th_hi

def betas_from_lower_subsample(lam_sub_hi, lam_sub_lo):
    lam_lo, lam_lo_delta = ru.get_lam0(lam_subsample=lam_sub_lo)
    lam_hi, lam_hi_delta = ru.get_lam0(lam_subsample=lam_sub_hi)
    basis_file = BASIS_DIR + "basis_fit_K-%d_V-%d_split-%s.pkl" % \
                 (NUM_BASES, len(lam_lo), SPLIT_TYPE)
    if os.path.exists(basis_file):
        print "grabbing file (%s) from CACHE, optimizing more!"%basis_file
        th, lam0, lam0_delta, parser = qfb.load_basis_fit(basis_file)
        betas_lo = parser.get(th, 'betas')
        betas_hi = np.array([np.interp(lam_hi, lam_lo, beta) for beta in betas_lo])
        return betas_hi
    else:
        return None


# WE ARE OPTIMIZING IN THE WHITENED SPACE, BUT DON'T SAVE IT IN THE WHITENED
# SPACE YOU DOPE
def save_unwhitened(th, lam0, lam0_delta, parser, K_chol, data_dir, split_type):
    # grab betas, hit em w/ K_chol, and save
    to_save = th.copy()
    betas  = parser.get(to_save, 'betas')
    parser.set(to_save, 'betas', np.dot(K_chol, betas.T).T)
    qfb.save_basis_fit(to_save, lam0, lam0_delta, parser, 
                           data_dir=BASIS_DIR, split_type=SPLIT_TYPE)

if __name__=="__main__":
    ##########################################################################
    ## set sampling parameters
    ##########################################################################
    narg              = len(sys.argv)
    #EXP_INT           = int(sys.argv[1]) if narg > 1 else 2
    #NUM_BASES, SPLIT_TYPE = EXP_PARAMS[EXP_INT]
    #SPLIT_TYPE        = sys.argv[1] if narg > 1 else SPLIT_TYPE
    #NUM_TRAIN_EXAMPLE = int(sys.argv[2]) if narg > 2 else NUM_TRAIN_EXAMPLE
    #MAX_LBFGS_ITER    = int(sys.argv[3]) if narg > 3 else MAX_LBFGS_ITER
    #NUM_BASES         = int(sys.argv[4]) if narg > 4 else NUM_BASES
    #BASIS_DIR         = sys.argv[5] if narg > 5 else BASIS_DIR
    NUM_BASES  = 4
    SPLIT_TYPE = "random"
    
    print \
"""
==============================================================================
===  preproc fit basis with params
==============================================================================
    SPLIT_TYPE        = {split}
    NUM_TRAIN_EXAMPLE = {ntrain}
    MAX_LBFGS_ITER    = {maxiter}
    NUM_BASES         = {num_bases}
    BETA_VARIANCE     = {beta_var}
    BETA_LENGTHSCALE  = {beta_ell}
    BASIS_DIR         = {bdir}
""".format(split = SPLIT_TYPE, ntrain=NUM_TRAIN_EXAMPLE, maxiter = MAX_LBFGS_ITER,
           num_bases = NUM_BASES, beta_var = BETA_VARIANCE, beta_ell = BETA_LENGTHSCALE,
           bdir = BASIS_DIR)

    # DR10 qso dataset and spec files
    qso_psf_flux, qso_psf_flux_ivar, qso_psf_mags, qso_z, spec_files, train_idx, test_idx = \
        ru.load_DR10QSO_train_test_idx(split_type = SPLIT_TYPE)

    # dig into the cache, grab the spec files
    CACHE_TRAIN_FILE = qfb.cache_file_name(SPLIT_TYPE, NUM_TRAIN_EXAMPLE)
    if not os.path.exists(CACHE_TRAIN_FILE):
        print "Cache file not there - quitting", CACHE_TRAIN_FILE
        sys.exit(1)
    handle    = open(CACHE_TRAIN_FILE, 'rb')
    train_idx_sub  = np.load(handle)
    spec_grid      = np.load(handle)
    spec_ivar_grid = np.load(handle)
    spec_mod_grid  = np.load(handle)
    unique_lams    = np.load(handle)
    spec_zs        = np.load(handle)
    spec_ids       = np.load(handle)
    handle.close()

    ## iterate over different lambda subsamples to get a quick starting
    ## point for more refined model
    lam_schedule = [5]
    for lam_idx, lam_subsample in enumerate(lam_schedule):
        print "========================================================="
        print " FITTING LAM SUBSAMPLE %d"%lam_subsample
        sys.stdout.flush()

        ## initialize a basis using existing eigenQuasar Model
        lam0, lam0_delta = ru.get_lam0(lam_subsample=lam_subsample,eigen_file = "")

        # resample spectra and spectra inverse variance onto common rest frame
        spectra_resampled, spectra_ivar_resampled, lam_mat = \
            ru.resample_rest_frame(spectra      = spec_grid,
                                   spectra_ivar = spec_ivar_grid, 
                                   zs           = spec_zs,
                                   lam_obs      = unique_lams, 
                                   lam0         = lam0)

        ## construct smooth + spiky prior over betas
        print "   Computing covariance cholesky "
        beta_kern = GPy.kern.Matern52(input_dim   = 1,
                                      variance    = BETA_VARIANCE,
                                      lengthscale = BETA_LENGTHSCALE)
        K_beta = beta_kern.K(lam0.reshape((-1, 1)))
        K_chol = np.linalg.cholesky(K_beta)

        ## clean nans in data and inverse variance 
        X                  = spectra_resampled
        X[np.isnan(X)]     = 0
        Lam                = spectra_ivar_resampled
        Lam[np.isnan(Lam)] = 0

        ## set up the likelihood and prior functions
        parser, loss_fun, loss_grad, prior_loss, prior_loss_grad  = \
            qfb.make_functions(X, Lam, lam0, lam0_delta,
                               K          = NUM_BASES,
                               K_chol     = K_chol,
                               sig2_omega = 1.,
                               sig2_mu    = 10.)

        ## initialize basis and weights
        Nspec, Vspec = spectra_resampled.shape
        if lam_idx > 0:
            th = initialize_from_lower_res(th_lo, lam_lo, parser_lo, lam0, parser)
        else: 
            th = np.zeros(parser.N)
            parser.set(th, 'betas', .01 * K_chol.dot(np.random.randn(Vspec, NUM_BASES)).T)
            parser.set(th, 'omegas', .01 * npr.randn(Nspec, NUM_BASES))
            parser.set(th, 'mus', .01 * npr.randn(Nspec))

        # already in cache (WE ARE OPTIMIZING IN CHOLESKY DECOMP MODE, SO 
        # WHITEN THE MATRIX)
        basis_file = "cache/basis_fits/basis_fit_K-%d_V-%d_split-%s.pkl"%(NUM_BASES, len(lam0), SPLIT_TYPE)
        if os.path.exists(basis_file):
            print "grabbing file (%s) from CACHE, optimizing more!"%basis_file
            th, lam0, lam0_delta, parser = qfb.load_basis_fit(basis_file)
            betas = parser.get(th, 'betas')
            betas_white = np.linalg.solve(K_chol, betas.T).T
            parser.set(th, 'betas', betas_white)

        betas_from_lo = betas_from_lower_subsample(lam_sub_hi = lam_subsample, 
                                                   lam_sub_lo = 10)
        if betas_from_lo is not None:
            print "betas from lo??"
            betas_white = np.linalg.solve(K_chol, betas_from_lo.T).T
            parser.set(th, 'betas', betas_white)

        ## make sure loss works
        print "Starting at loss: %2.5g"%(loss_fun(th) + prior_loss(th))
        def full_loss_grad(th, idx=None):
            return loss_grad(th, idx) + prior_loss_grad(th, idx)

        ## sanity check gradient
        check_grad(fun = lambda th: loss_fun(th) + prior_loss(th), # X, Lam), 
                   jac = lambda th: full_loss_grad(th),
                   th  = th)

        ######################################################################
        #### Beat on RMS Prop for a while 
        ######################################################################
        obj_vals = []
        min_val = np.inf
        min_x   = None
        def callback(x, i, g): 
            global min_val
            global min_x
            global obj_vals
            if i % lam_subsample == 0:
                loss_val = loss_fun(x) + prior_loss(x)
                if loss_val < min_val:
                    min_x = x.copy()
                    min_val = loss_val
                print " %d, loss = %2.7g, grad = %2.4g " % \
                    (i, loss_val, np.sqrt(np.dot(g,g)))
                sys.stdout.flush()
                obj_vals.append(loss_val)

        step_sizes = np.logspace(-1, -4.5, 16)
        momentums  = np.logspace(-.1, -2.5, 16)
        for step_size, momentum in zip(step_sizes, momentums):
            print " === step_size = %2.6g, momentum = %2.6g ==="%(step_size, momentum)
            rms_prop(full_loss_grad, x=th, callback=callback,
                     num_iters = lam_subsample * 5,
                     step_size = step_size,
                     gamma     = momentum,
                     eps       = 1e-9)
            th = min_x
        save_unwhitened(th, lam0, lam0_delta, parser, K_chol,
                        data_dir   = BASIS_DIR,
                        split_type = SPLIT_TYPE)

        ## tighten it up a bit
        def chunk_callback(x, i):
            g = full_loss_grad(x)
            print "    chunk %d grad mag = %2.4g " % \
                    (i, np.sqrt(np.dot(g,g)))
            sys.stdout.flush() 
            if i%10 == 0:
                print "    .... writing out chunk %d result to disk"%i
                save_unwhitened(x, lam0, lam0_delta, parser, K_chol,
                                data_dir   = BASIS_DIR,
                                split_type = SPLIT_TYPE)

        res = minimize_chunk(fun = lambda th: loss_fun(th) + prior_loss(th),
                             jac = lambda th: loss_grad(th) + prior_loss_grad(th),
                             x0  = th,
                             max_iter   = MAX_LBFGS_ITER,
                             method     = 'L-BFGS-B',
                             chunk_size = 50,
                             callback   = chunk_callback, 
                             verbose    = False)
        th = res.x

        ######################################################################
        # keep parser, lam, th as lower res
        ######################################################################
        parser_lo = parser
        lam_lo    = lam0
        th_lo     = th

        # save result
        th_mle = res.x
        ll_mle = loss_fun(th_mle) + prior_loss(th_mle)

        ## plot random profiles
        #plot a handful of random directions
        #if False:
        #    egrid = np.linspace(-1.5, 1.5, 30)
        #    def gen_random_profile(th): 
        #        rdir     = npr.randn(th.shape[0])
        #        rdir     = rdir / np.sqrt(np.sum(rdir**2))
        #        rdirloss = lambda(e): loss_fun(th + e*rdir) + prior_loss(th + e*rdir)
        #        lgrid = np.array([rdirloss(e) for e in egrid])
        #        return lgrid

        #    plt.figure(1)
        #    for i in range(5):
        #        lprof = gen_random_profile(th_mle)
        #        plt.plot(egrid, lprof)
        #    plt.vlines(x = 0, ymin = lprof.min(), ymax = lprof.max(), linewidth=4)
        #    plt.title("objective function, random directions")
        #    plt.savefig("obj_fun_dirs_lamsample_%d.pdf"%lam_subsample, bbox_inches='tight')
        #    plt.close("all")

    # exponentiate and normalize params
    if False:
        betas  = parser.get(th, 'betas')
        omegas = parser.get(th, 'omegas')
        mus    = parser.get(th, 'mus')
        W = np.exp(omegas)
        W = W / np.sum(W, axis=1, keepdims=True)
        B = np.exp(np.dot(K_chol, betas.T).T)
        B = B / np.sum(B * lam0_delta, axis=1, keepdims=True)
        M = np.exp(mus)
        Xtilde = np.dot(W*M, B)

        ex_idx = 10
        plt.plot(lam0, X[ex_idx,:])
        plt.plot(lam0, Xtilde[ex_idx,:])
        plt.show()


#def minibatch_minimize(grad, x, N_example, num_epochs=100, batch_size=10000,
#                        callback=None, step_size=0.1, mass=0.9, eps=1e-8):
#
#    # set initial step size and masses and running average sq grad
#    step_size0 = step_size
#    mass0      = mass
#    avg_sq_grad = np.ones(len(x)) # Is this really a sensible initialization?
#    cnt = 1
#    for epoch in xrange(num_epochs):
#
#        # full grad once an epoch (this might be slow)
#        g = grad(x)
#        if callback: callback(x, epoch, g)
#        print "mass, step = %2.2f, %2.2f"%(mass, step_size)
#
#        # divide up
#        idxs        = npr.permutation(N_example)
#        num_batches = int(np.ceil(N_example / float(batch_size)))
#        for batch_i in xrange(num_batches):
#            # grab minibatch
#            starti    = batch_i * batch_size
#            endi      = min(starti + batch_size, N_example)
#            batch_idx = idxs[starti:endi]
#
#            g = grad(x, batch_idx)
#            avg_sq_grad = avg_sq_grad * mass + g**2 * (1 - mass)
#            x -= step_size * g/(np.sqrt(avg_sq_grad) + eps)           
#
#            # update step size
#            step_size = step_size0 / np.power((1. + step_size0 * cnt), .75)
#            mass      = mass0 / np.power((1. + mass0 * cnt), .9)
#            cnt += 1
#    return x


