# Class for handling images and parameters given in fits files. Implements
# transformation between 
#
# Author: Andrew Miller <acm@seas.harvard.edu>
import fitsio
import numpy as np
import scipy.stats
from util.bound.bounding_box import calc_bounding_radius

# autograd array wrapper
from autograd import grad

color = {'u': 0, 'g': 1, 'r': 2, 'i': 3, 'z': 4}

class FitsImage():
    """ FitsImage - simple organization of fits file images that 
        Dustin has been passing us.  Each FitsImage maintains it's own 
        header information, most importantly: 

         Camera Orientation Information
          - wcs : astropy.wcs object - used to go between pixel and equatorial 
                  coordinates.  Obviates the following three fields (which 
                  are held onto for testing)
          - rho = (rho_x, rho_y)    : pixel reference point
          - phi = (phi_ra, phi_dec) : reference point in equatorial coord
          - Ups = 2x2 matrix : Projection matrix that takes you from pixel to 
                  equa coordinates

         Camera Point Spread Function (modeled as a mixture of gaussians)
          - weights : MoG weights
          - means   : MoG means (X and/or Y seem to be negated in this model
          - covars  : 2x2 Covariance vectors for PSF MoG
          - astrans : optional polynomial model for large areas of the sky

         Calibration Information
          - kappa : gain added after the fact
          - epsilon : expected number of electrons (per pixel) not due to 
                      sources that are modeled
          - darkvar : dark variance (TODO: where does this come into the likelihood?)
          - calib   : calibration value (nanomaggies per count) for the image

         Image Signal Information
          - dn = NxM array    : data number array
          - nelec = NxM array : number of electrons corresponding to each 
                                pixel, indexed nelec[y, x]
    """
    def __init__(self, band, 
            fits_file_template = None,
            timg               = None,
            exposure_num       = 0,
            calib              = None,
            gain               = None,
            darkvar            = None,
            sky                = None,
            astrans            = None): 
        self.band      = band
        if fits_file_template:
            self.band_file = fits_file_template%band
            self.img       = fitsio.FITS(self.band_file)[exposure_num].read()
            header         = fitsio.read_header(self.band_file, ext=exposure_num)
        elif timg:
            self.band_file = None
            self.img = timg[0].getImage()
            header = timg[1]['hdr']
        else:
            pass

        self.header = header
        self.astrans = astrans

        # Compute the number of electrons, resource: 
        # http://data.sdss3.org/datamodel/files/BOSS_PHOTOOBJ/frames/RERUN/RUN/CAMCOL/frame.html
        # (Neither of these look like integers)
        if fits_file_template:
            self.dn    = self.img / header["CALIB"] + header["SKY"]
            self.nelec = np.round(self.dn * header["GAIN"])
        else:
            # TODO(awu): what are CALIB and GAIN?
            self.dn    = self.img / calib + sky #timg[0].getSky().val
            self.nelec = np.round(self.dn * gain)

        self.shape = self.nelec.shape
        self.pixel_grid = self.make_pixel_grid()  # keep pixel grid around

        # reference points
        # TODO: Does CRPIX1 refer to the first axis of self.img ?? 
        self.rho_n = np.array([header['CRPIX1'], header['CRPIX2']])  # PIXEL REFERENCE POINT
        self.phi_n = np.array([header['CRVAL1'], header['CRVAL2']])  # EQUA REFERENCE POINT
        self.Ups_n = np.array([[header['CD1_1'], header['CD1_2']],   # MATRIX takes you into EQUA TANGENT PLANE
                               [header['CD2_1'], header['CD2_2']]])
        self.Ups_n_inv = np.linalg.inv(self.Ups_n)

        ###### DEAD CODE ######################################################
        #astrometry wcs object for pixel x,y to equa ra,dec conversion
        #self.wcs = WCS(self.band_file) #Tan(self.band_file)
        #######################################################################

        # set image specific KAPPA and epsilon 
        if fits_file_template:
            self.kappa    = header['GAIN']     # TODO is this right??
            self.epsilon  = header['SKY'] * self.kappa # background rate
            self.epsilon0 = self.epsilon      # background rate copy (for debuggin)
            self.darkvar  = header['DARKVAR']  # also eventually contributes to mean?
            self.calib    = header['CALIB']    # dn = nmaggies / calib, calib is NMGY
        else:
            self.kappa = gain
            self.epsilon = timg[0].sky.val * self.kappa
            self.epsilon0 = self.epsilon
            self.darkvar = darkvar
            self.calib = calib

        # point spread function
        if fits_file_template:
            psfvec       = [header['PSF_P%d'%i] for i in range(18)]
        else:
            psfvec       = [psf for psf in timg[0].getPsf()]

        self.weights = np.array(psfvec[0:3])
        self.means   = np.array(psfvec[3:9]).reshape(3, 2)  # one comp mean per row
        covars       = np.array(psfvec[9:]).reshape(3, 3)   # [var_k(x), var_k(y), cov_k(x,y)] per row
        self.covars  = np.zeros((3, 2, 2))
        self.invcovars = np.zeros((3, 2, 2))
        self.logdets   = np.zeros(3)
        for i in range(3):
            self.covars[i,:,:]    = np.array([[ covars[i,0],  covars[i,2]],
                                              [ covars[i,2],  covars[i,1]]])

            # cache inverse covariance 
            self.invcovars[i,:,:] = np.linalg.inv(self.covars[i,:,:])

            # cache log determinant
            sign, logdet = np.linalg.slogdet(self.covars[i,:,:])
            self.logdets[i] = logdet

        # for a point source in this image, calculate the radius such that 
        # at least 99% of photons from that source will fall within
        ERROR = 0.001
        self.R = calc_bounding_radius(self.weights,
                                      self.means,
                                      self.covars,
                                      ERROR)

    def contains(self, s_equa, pad = 50):
        """ can this source be seen by this image? 
          s_equa : equatorial locations of the point in the sky in question
          pad    : how many pixels outside of the image do we include in 'contains'?
        """
        v_s = self.equa2pixel(s_equa)
        return (v_s[0] > -pad) and (v_s[0] < self.nelec.shape[0] + pad) and \
               (v_s[1] > -pad) and (v_s[1] < self.nelec.shape[1] + pad)

    def equa2pixel(self, s_equa):
        if self.astrans:
            return np.array(self.astrans.radec_to_pixel(s_equa[0], s_equa[1]))

        phi1rad = self.phi_n[1] / 180. * np.pi
        s_iwc = np.array([ (s_equa[0] - self.phi_n[0]) * np.cos(phi1rad),
                                   (s_equa[1] - self.phi_n[1]) ])
        s_pix = np.dot(self.Ups_n_inv, s_iwc) + self.rho_n
        return s_pix

    def pixel2equa(self, s_pixel):
        if self.astrans:
            c = color[self.band]
            return np.array(self.astrans.pixel_to_radec(s_pixel[0],
                                                        s_pixel[1],
                                                        c))

        phi1rad = self.phi_n[1] / 180. * np.pi
        s_iwc   = np.dot(self.Ups_n, s_pixel - self.rho_n) 
        s_equa = np.array([ s_iwc[0]/np.cos(phi1rad) + self.phi_n[0], 
                                    s_iwc[1] + self.phi_n[1] ])
        return s_equa

    def nmgy2counts(self, flux):
        return (flux / self.calib) * self.kappa

    def make_pixel_grid(self):
        """ makes a stack of points corresponding to each point in a pixel grid
            with input shape 
        """
        y_grid = np.arange(self.nelec.shape[0], dtype=np.float) + 1
        x_grid = np.arange(self.nelec.shape[1], dtype=np.float) + 1
        xx, yy = np.meshgrid(x_grid, y_grid, indexing='xy')
        # whenever we flatten and reshape use C ordering...
        return np.column_stack((xx.ravel(order='C'), yy.ravel(order='C')))

