import numpy as np
import matplotlib.pyplot as plt

from celeste import *
from mcmc_utils import *
from mcmc_transitions import *

im = FitsImage('r')

# MCMC step
rand = np.random.RandomState(seed=89) # http://xkcd.com/221/

from scipy.misc import imsave

srcs = np.array([])
imsave("orig_image.png", im.nelec)
srcs = initializeSources(srcs, im, percentile = 95)

imsave("init_image.png", gen_model_image(srcs, im))
createImageDifference("initialize_im_diff.png", im.nelec, gen_model_image(srcs, im).copy())
print srcs.size

Niters = 1
logprobs = np.zeros(Niters + 1)
logprobs[0] = celeste_likelihood(srcs, im)

for it in xrange(Niters):
    # Gibbs sampling for brightness for each catalog entry
    # http://andrewgelman.com/2011/12/07/neutral-noninformative-and-informative-conjugate-beta-and-gamma-prior-distributions/

    im1 = gen_model_image(srcs, im).copy()
    srcs = gibbsSampleBrightnesses(srcs, im, aPrior=1./3, bPrior=0.1, eta=1, rand=rand)
    im2 = gen_model_image(srcs, im).copy()

    createImageDifference("gibbs_im_diff_%d.png" % it, im1, im2)

    #for i in xrange(len(srcs)):
        # TODO: randomize order
    #    sliceSampleSourceSingleAxis(srcs, im, i, 0, rand=rand)
    #    sliceSampleSourceSingleAxis(srcs, im, i, 1, rand=rand)

    im3 = gen_model_image(srcs, im).copy()
    createImageDifference("slice_im_diff_%d.png" % it, im2, im3)

    if rand.rand() > 0.5:
        srcs = splitStar(srcs, im, rand=rand)
    else:
        srcs = mergeStar(srcs, im, rand=rand)

    if rand.rand() > 0.5:
        srcs = birthStar(srcs, im, rand=rand)
    else:
        srcs = deathStar(srcs, im, rand=rand)

    logprobs[it+1] = logprob = celeste_likelihood(srcs, im)
    print "After iteration %s: %s, cat len %s" % (it, logprob, len(srcs))
    print "Brightnesses:", [src.b for src in srcs]

plt.plot(logprobs)
plt.show()
