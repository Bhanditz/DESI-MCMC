# Authors: Andrew Miller acm@seas.harvard.edu
import planck
import numpy as np
import CelestePy.celeste as celeste
import CelestePy.celeste_galaxy_conditionals as gal
from CelestePy import BANDS
from util.misc.plot_util import compare_to_model, subplot_imshow_colorbar
from util.infer.slicesample import slicesample
from util.infer.hmc import hmc
from util.misc.timer_util import *
import matplotlib.pyplot as plt

def celeste_gibbs_sample(srcs, imgs, subiter=2, debug=False, verbose=True): 
    """ Function to do a single gibbs sweep over a list of sources and images

        1. Firstly, it samples source-photon "responsibilities" for each pixel,

            Z_{n,m,0}, ..., Z_{n,m,S} | \Theta, {a} ~ Mult(p_0, \dots, p_S, x_{n,m})

           where s = 0 is reserved for constant noise over the entire image, and 
           S is the total number of sources, and the probabilities are base

        2. Secondly, it samples individual source parameters conditioned on the 
           source-specific photons.  This is specific to source type

            Theta_s | a_s, { Z_{n,m,s} }_{N,M} \propto p(Z_{n,m,s} | Theta_s) p(theta_s)

           Separate methods are implemented for the Star, Gal, QSO source 
           types

        3. Thirdly, it samples TYPE of source (galaxy, star, or one day, quasar)

            a_s | Z_s, Theta_s \propto p(Z_s | Theta_s, a_s) p(a_s)

           This has to be done with a reversible jump move

        Input: 
            srcs    : python list of SrcParams
            imgs    : python list of FitsImage objects
            debug   : turn on plotting
            verbose : turn on printing
    """

    ##
    ## 1.  for each image, sample the source specific counts (Z_{n,m,s})
    ##
    printif("    sampling Z's", verbose)
    all_src_images = []
    for img in imgs:
        src_probs = celeste.gen_src_prob_layers(srcs, img)
        src_image = np.zeros(src_probs.shape)
        for (i,j), xij in np.ndenumerate(img.nelec):

            # 
            possible_sources = get_bounding_boxes_idx(pixel_loc, src_boxes)


            src_image[:,i,j] = np.random.multinomial(int(xij), src_probs[:,i,j])
        all_src_images.append(src_image)

        #### debug #####
        if False:
            if img.band == 'r':
                fig, axarr = plt.subplots(1, src_image.shape[0])
                subplot_imshow_colorbar(src_image, fig, axarr)
                plt.show()
        #### end debug ####
    printif("      .... done", verbose)

    ##
    ## 1a. compute optimal noise param for each image
    ##
    printif("    sampling image specific epsilons", verbose)
    a_0 = 5      # convolution parameter - higher tends to avoid 0
    b_0 = .005   # inverse scale parameter
    for i,img in enumerate(imgs):
        a_n         = a_0 + np.sum(all_src_images[i][0,:,:])
        b_n         = b_0 + img.nelec.size
        eps_tmp     = img.epsilon
        img.epsilon = np.random.gamma(a_n, 1./b_n)
        printif("      img %d eps %2.2f => %2.2f (eps0 = %2.2f)" % \
                (i, eps_tmp, img.epsilon, img.epsilon0),
                verbose and i < 5)
    printif("      .... done", verbose)

    ##
    ## 2. for each source, sample source specific params
    ##
    for s in range(len(srcs)):

        # inplace re-sample src specific parameters
        s_images = [ simg[s+1,:,:] for simg in all_src_images ]
        sample_source_params(srcs[s], s_images, imgs, verbose=verbose)

    return None


def sample_source_photons_single_image_cython(img, srcs):
    """Given a single photon-count image and a list of sources, sample
    source-specific images using the poisson/multinomial representation

    args:
        img: FitsImage
        srcs: python list of srcs
    returns:
        src_imgs: python list of src images
        noise_count: sampled noise
    """

    def src_image_bound(src, img):
        """ get radius for this source/image pair that encompasses 1-epsilon 
        of photons generated"""
        if src.a == 0:
            return img.R
        else:
            return gal.gen_galaxy_psf_image_bound(src, img)

    # compute source boxes
    src_locs  = np.row_stack([img.equa2pixel(s.u) for s in srcs])
    imgR      = np.array([src_image_bound(s, img) for s in srcs])
    src_boxes0 = np.column_stack([
                    np.floor(src_locs[:,0] - imgR),
                    np.ceil(src_locs[:,0] + imgR),
                    np.floor(src_locs[:,1] - imgR),
                    np.ceil(src_locs[:,1] + imgR)
                    ])

    import CelestePy.celeste_sample_sources as css
    # generate model image for each source
    src_imgs  = [css.NativePatch(*celeste.gen_src_image_with_fluxes(s, img))
                 for s in srcs]

    # create src boxes that are exact to eliminate checking
    src_boxes = np.zeros((len(src_imgs), 4), np.ulonglong)
    for s, si in enumerate(src_imgs):
       src_boxes[s,:] = (si.x0, si.x1, si.y0, si.y1)

    # sampled images
    samp_imgs = [css.NativePatch(np.zeros(s.data.shape), (s.y0, s.y1), (s.x0, s.x1))
                 for s in src_imgs]

    samp_imgs, noise_sum = css.sample_source_counts(
            src_imgs  = src_imgs,
            samp_imgs = samp_imgs,
            nelec     = np.array(img.nelec, dtype=np.float),
            src_boxes = src_boxes,
            epsilon   = img.epsilon,
            random_state = np.random.RandomState()
        )
    return samp_imgs, noise_sum


def sample_source_params(src, src_imgs, imgs, verbose):
    """ In place sampling of source parameters - switches on type and 
    calls source type specific samplers"""

    # sample star or galax params
    if src.a == 0: 
        sample_star_params(src, src_imgs, imgs, subiter=2, verbose=verbose)
    elif src.a == 1:
        sample_galaxy_params(src, src_imgs, imgs, subiter=2, verbose=verbose)
    else:
        raise Exception("Must be star or galaxy to sample")


p_cached = None
def sample_galaxy_params(src, src_imgs, imgs, subiter=2, verbose=False):
    """ Samples theta_gal conditioned on a_s = 1, and Z_s (source-specific
        photons 
    """

    def sample_galaxy_fluxes():
        """ samples fluxes, u,g,r,i,z, given all other parameters

              p(b | z, theta) \propto p(z | b, theta) p(b | theta)
                              =       pois(sum z | b, theta) p(b | theta)

        """
        # prior params
        a_0 = 5.     # convolution parameter - higher tends to avoid 0
        b_0 = .005   # inverse scale parameter

        # sum detectable photons 
        bands       = ['u', 'g', 'r', 'i', 'z']
        band_counts = np.zeros(5)
        psf_sums    = np.zeros(5)
        for n,img in enumerate(imgs):

            # identify which band this image came from
            bi = bands.index(img.band)

            # compute for each band sum_{n_band} sum_{m in n} Z_{n,m,s}
            band_counts[bi] += src_imgs[n].sum()

            # compute for each band sum_{n_band} sum_{m in n} f_{n,m,s}
            psf_ns = gen_galaxy_psf_image(src, img)
            psf_sums[bi] += psf_ns.sum() * img.kappa/img.calib  # this will be close to one if image contains whole galaxy

        # debug printer
        #if True:
        #    print band_counts
        #    print psf_sums 

        # sample using conjugate prior
        a_n        = a_0 + band_counts
        b_n        = b_0 + psf_sums
        fluxes     = np.random.gamma(a_n, 1./b_n)
        src.fluxes = dict(zip(bands, fluxes))

    def skew_likelihood(th): 
        """ conditional log likelihood of skew parameters given everything
            else 
        """
        llprior = gal.galaxy_shape_prior_constrained(th[0], th[1], th[2], th[3])
        if not np.isfinite(llprior):
            return -np.inf
        return gal.galaxy_skew_like(th,
                                    src.u,
                                    src.fluxes,
                                    Z_s    = src_imgs,
                                    images = imgs,
                                    unconstrained = False) + llprior

    def slice_sample_skew(): 
        th_curr = np.array([src.theta, src.sigma, src.phi, src.rho])
        for i in range(1):
            #th_curr, llh = slicesample(
                #xx       = th_curr,
                #llh_func = lambda(th): skew_likelihood,
                #lb = np.array([0., 0., -np.pi, .01]),
                #ub = np.array([1., 200., 2*np.pi, 1.]),
                #step = 1.)
            th_curr, llh = slicesample(init_x = th_curr,
                                       logprob = skew_likelihood,
                                       step_out = True,
                                       doubling_step = True,
                                       numdir = len(th_curr),
                                       compwise = False)

            # clamp th_curr to 0, np.pi
            th_curr[2] = (th_curr[2] + np.pi)%np.pi
        src.theta, src.sigma, src.phi, src.rho = th_curr

    def hmc_sample_skew():
        """ samples sig, rho, phi and theta conditioned on src images and
            band fluxes
        """
        def skew_likelihood_grad(th):
            grad = np.zeros(len(th))
            for i in range(len(grad)):
                de = np.zeros(len(th))
                de[i] = 1e-5
                grad[i] = (skew_likelihood(th + de) - skew_likelihood(th - de)) / 2e-5
            return grad

        # hmc sample rho
        step_sz          = 5e-4
        adapt_step       = False
        avg_accept_rate  = .8
        STEPS_PER_SAMPLE = 50
        #th_curr = np.array( gal.unconstrain_params([ src.theta,
        #                                             src.sigma,
        #                                             src.phi,
        #                                             src.rho ]) )

        th_curr = np.array([src.theta, src.sigma, src.phi, src.rho])
        th_samp, step_sz, avg_accept_rate = hmc(
            U        = skew_likelihood,
            grad_U   = skew_likelihood_grad,
            step_sz  = step_sz,
            n_steps  = STEPS_PER_SAMPLE,
            q_curr   = th_curr,
            negative_log_prob = False,
            adaptive_step_sz  = adapt_step,
            min_step_sz       = 0.00005,
            avg_accept_rate   = avg_accept_rate,
            tgt_accept_rate   = .65)
        #src.theta, src.sigma, src.phi, src.rho = gal.constrain_params(th_samp)
        src.theta, src.sigma, src.phi, src.rho = th_samp

    def hmc_sample_galaxy_params():
        # make sure likelihood evaluates
        ll_fun = lambda(th): gal.galaxy_source_like(th, src_imgs, imgs, check_overlap=True, unconstrained=True)
        ll_jac = lambda(th): gal.galaxy_source_like_grad(th, src_imgs, imgs)

        # hmc sample rho
        step_sz          = 1e-5
        avg_accept_rate  = .8
        STEPS_PER_SAMPLE = 50
        th_curr = np.array([src.theta, src.sigma, src.phi, src.rho])
        th = np.concatenate((
                [src.theta, src.sigma, src.phi, src.rho],
                src.u,
                [src.fluxes[b] for b in BANDS]))
        mass = np.array([ .1, .1, 1., .1,    #skews
                          .001, .001,        #locs 
                          1., 1., 1., 1., 1. #fluxes
                        ]) 

        global p_cached
        if p_cached is None:
            p_cached = np.sqrt(mass) * np.random.randn(th.shape[0])

        print p_cached
        th_samp, p_samp, step_sz, avg_accept_rate = hmc(
            x_curr           = th,
            llhfunc          = ll_fun,
            grad_llhfunc     = ll_jac,
            eps              = step_sz,
            num_steps        = STEPS_PER_SAMPLE,
            mass             = mass,
            p_curr           = p_cached,
            refresh_alpha    = .9,
            adaptive_step_sz = False)

        if np.all(th_samp==th):
            print "HMC (flux, skew) proposal rejected!!"

        #src.theta, src.sigma3, src.phi, src.rho = gal.constrain_params(th_samp)
        src.theta, src.sigma, src.phi, src.rho = th_samp[0:4]
        src.u = th_samp[4:6]
        src.fluxes = dict(zip(BANDS, th_samp[6:]))
        p_cached = p_samp

    ## iterate a bunch - sample fluxes, scaling/rotation, and then location
    for gibbs_iter in range(subiter):
        sample_galaxy_fluxes()
        #hmc_sample_skew()

        # work a hmc sample in
        hmc_sample_galaxy_params()


def sample_star_params(src, src_images, imgs, subiter=2, verbose=False):
    #### iterate a bunch - sample locs and brightness/temps
    for gibbs_iter in range(subiter):

        # cache existing state
        tmp_t, tmp_b = src.t, src.b

        # compute fraction of photons this image will see
        sum_fs   = np.zeros(len(imgs))  
        for n, img in enumerate(imgs):
            sum_fs[n] = min(1., np.sum(gen_point_source_psf_image(src.u, img)))

        # sample TEMP and BRIGHTNESS conditioned on U
        src_image_sums = np.array([ img.sum() for img in src_images ])
        for it in range(2):
            th     = np.array([src.t, src.b])
            th, ll = slicesample(
                xx       = th,
                llh_func = lambda(th): temp_bright_like(th, sum_fs, src_image_sums, imgs),
                step     = [1000, .1],
                step_out = True,
                x_l      = [0., 0.])
        src.t, src.b = th[0], th[1]
        printif("        t: %2.2f => %2.2f"%(tmp_t, src.t),
                verbose and s < 5)
        printif("        b: %.4g  => %.4g"%(tmp_b, src.b), 
                verbose and s < 5)

        # sample U conditioned on TEMP and BRIGHTNESS
        tmp_u = src.u
        u, ll = slicesample(xx       = src.u,
                            llh_func = lambda(u): loc_like(u, src, imgs, src_images),
                            step     = [.1, .1], 
                            step_out = False)
        src.u = u
        printif("        u: (%.4g, %4g) => (%.4g, %.4g)"%(tmp_u[0], tmp_u[1], u[0], u[1]), 
                verbose and s < 5)


#### joint Temp, Brightness source specific opt func
def temp_bright_like(th, fs_sum, src_image_sums, imgs):
    ll = 0
    for i, img in enumerate(imgs): 
        expected_num_photons = fs_sum[i] * \
            planck.photons_expected_brightness(th[0], th[1], img.band)
        if expected_num_photons > 0:
            ll += np.sum(src_image_sums[i]) * \
                  np.log(expected_num_photons) - expected_num_photons

    # prior over temperature, prior over brightness
    #ll += gamma(4, scale=1/.0005).logpdf(th[0])
    #ll += gamma(1., scale=1.).logpdf(th[1])
    #ll += fast_gamma_lnpdf(th[0], 4., .0005)
    #ll += fast_gamma_lnpdf(th[1], 1., 1.)
    ll += unif_lnpdf(th[0], 20., 20000.)
    ll += unif_lnpdf(th[1], 0., 1.)
    return ll

#### likelihood factor that only depends on locatio
def loc_like(u, src, imgs, src_images): 
    ll = 0
    src.u = u
    for n, img in enumerate(imgs):
        if not img.contains(src.u):
            continue
        f_sn = gen_src_image(src, img)

        # mask to remove the negative infinities
        mask = (src_images[n] > 0) & (f_sn > 0)
        ll  += np.sum(np.log(f_sn[mask])*src_images[n][mask]) - f_sn.sum()
    return ll

def unif_lnpdf(x, a0, b0):
    if x <= a0 or x >= b0:
      return -np.inf
    return 0.

def printif(statement, condition):
    if condition:
        print statement


