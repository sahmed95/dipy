""" Classes and functions for fitting ivim model """
from __future__ import division, print_function, absolute_import

from distutils.version import LooseVersion

import numpy as np
import scipy
from dipy.core.gradients import gradient_table
from dipy.reconst.base import ReconstModel
from dipy.reconst.multi_voxel import multi_voxel_fit
from dipy.core.optimize import Optimizer

SCIPY_LESS_0_17 = LooseVersion(scipy.version.short_version) < '0.17'

if SCIPY_LESS_0_17:
    leastsq = scipy.optimize.leastsq
else:
    least_squares = scipy.optimize.least_squares


def ivim_prediction(params, gtab, S0=1.):
    """The Intravoxel incoherent motion (IVIM) model function.

    Parameters
    ----------
    params : array
        An array of IVIM parameters - [S0, f, D_star, D]

    gtab : GradientTable class instance
        Gradient directions and bvalues

    S0 : float, optional
        This has been added just for consistency with the existing
        API. Unlike other models, IVIM predicts S0 and this is over written
        by the S0 value in params.

    References
    ----------
    .. [1] Le Bihan, Denis, et al. "Separation of diffusion
               and perfusion in intravoxel incoherent motion MR
               imaging." Radiology 168.2 (1988): 497-505.
    .. [2] Federau, Christian, et al. "Quantitative measurement
               of brain perfusion with intravoxel incoherent motion
               MR imaging." Radiology 265.3 (2012): 874-881.
    """
    S0, f, D_star, D = params
    b = gtab.bvals
    S = S0 * (f * np.exp(-b * D_star) + (1 - f) * np.exp(-b * D))
    return S


def _ivim_error(params, gtab, signal):
    """Error function to be used in fitting the IVIM model.

    Parameters
    ----------
    params : array
        An array of IVIM parameters. [S0, f, D_star, D]

    gtab : GradientTable class instance
        Gradient directions and bvalues.

    signal : array
        Array containing the actual signal values.

    """
    return signal - ivim_prediction(params, gtab)


def _ivim_error_regularized(params, gtab, signal, reg=[1., .20, 0.001, 0.0001]):
    """
    Regularized error function to be used in fitting the IVIM model.

    Parameters
    ----------
    params : array
        An array of IVIM parameters. [S0, f, D_star, D]

    gtab : GradientTable class instance
        Gradient directions and bvalues.

    signal : array
        Array containing the actual signal values.

    reg : array
        Array containing regularization parameters.
        Default : [1000., .20, 0.001, 0.0001]
    """
    return np.sum((signal - ivim_prediction(params, gtab) ** 2)) + np.sum((params - reg) ** 2)


def f_D_star_prediction(params, gtab, x0):
    """Function used to predict f and D_star when S0 and D are known.

    This restricts the value of 'f' and 'D_star' to physically feasible
    solutions since a direct fitting leads to 'f' jumping to very high
    values and 'D_star' vanishing.

    Parameters
    ----------
    params : array, dtype=float
        An array containg the values of f and D_star.

    gtab : GradientTable class instance
        Gradient directions and bvalues.

    other_params : array, dtype=float
        The parameters S0 and D which are obtained from a linear fit.
    """
    f, D_star = params
    S0, D = x0[0], x0[3]
    b = gtab.bvals
    S = S0 * (f * np.exp(-b * D_star) + (1 - f) * np.exp(-b * D))
    return S


def f_D_star_error(params, gtab, signal, x0):
    """Error function used to fit f and D_star keeping S0 and D fixed

    Parameters
    ----------
    params : array
        The value of f and D_star.

    gtab : GradientTable class instance
        Gradient directions and bvalues.

    signal : array
        Array containing the actual signal values.

    other_params : array
        The parameters S0 and D which are fixed and obtained from a linear fit.
    """
    f, D_star = params
    S0, D = x0[0], x0[3]

    return signal - f_D_star_prediction([f, D_star], gtab, x0)


class IvimModel(ReconstModel):
    """Ivim model
    """

    def __init__(self, gtab, split_b_D=400.0, split_b_S0=200., method="two_stage",
                 bounds=None, tol=1e-15,
                 options={'gtol': 1e-15, 'ftol': 1e-15,
                          'eps': 1e-15, 'maxiter': 1000}):
        """
        Initialize an IVIM model.

        The IVIM model assumes that biological tissue includes a volume
        fraction 'f' of water flowing in perfused capillaries, with a
        perfusion coefficient D* and a fraction (1-f) of static (diffusion
        only), intra and extracellular water, with a diffusion coefficient
        D. In this model the echo attenuation of a signal in a single voxel
        can be written as

            .. math::

            S(b) = S_0[f*e^{(-b*D\*)} + (1-f)e^{(-b*D)}]

            Where:
            .. math::

            S_0, f, D\* and D are the IVIM parameters.

        Parameters
        ----------
        gtab : GradientTable class instance
            Gradient directions and bvalues

        split_b_D : float, optional
            The b-value to split the data on for two-stage fit. This will be
            used while estimating the value of D. The assumption is that at
            higher b values the effects of perfusion is less and hence the
            signal can be approximated as a mono-exponenetial decay.
            default : 400.

        split_b_S0 : float, optional
            The b-value to split the data on for two-stage fit for estimation
            of S0 and initial guess for D_star. The assumption here is that
            at low bvalues the effects of perfusion are more.
            default : 200.

        method : str, optional
            One of either 'two_stage' or 'one_stage'.

            'one_stage' fits using the following method : 
                Linear fitting for D (bvals > 200) and store S0_prime.
                Another linear fit for S0 (bvals < 200).
                Estimate f using 1 - S0_prime/S0.
                Use least squares to fit only D_star. 

            'two_stage' performs another fitting using the parameters obtained
            in 'one_stage'. This method gives a roboust fit.

            These two methods were adopted since a straight forward fitting
            gives solutions which are not physical (negative values of f, D_star, D).
            For some regions the solution jumps to either D=0 or D_star=0
            giving unreasonable values for f. In Federau's paper, f values > 0.3
            and D_star values > 0.05 are discarded.

            default : 'two_stage'

        bounds : tuple of arrays with 4 elements, optional
            Bounds to constrain the fitted model parameters. This is only supported for
            Scipy version > 0.17. When using a older scipy version, this function will raise
            an error if bounds are different from None.
            default : ([0., 0., 0., 0.], [np.inf, 1., 1., 1.])

        tol : float, optional
            Tolerance for convergence of minimization.
            default : 1e-7

        options : dict, optional
            Dictionary containing gtol, ftol, eps and maxiter. This is passed
            to leastsq.
            default : options={'gtol': 1e-7, 'ftol': 1e-7, 'eps': 1e-7, 'maxiter': 1000}

        References
        ----------
        .. [1] Le Bihan, Denis, et al. "Separation of diffusion
                   and perfusion in intravoxel incoherent motion MR
                   imaging." Radiology 168.2 (1988): 497-505.
        .. [2] Federau, Christian, et al. "Quantitative measurement
                   of brain perfusion with intravoxel incoherent motion
                   MR imaging." Radiology 265.3 (2012): 874-881.
        """
        if not np.any(gtab.b0s_mask):
            e_s = "No measured signal at bvalue == 0."
            e_s += "The IVIM model requires signal measured at 0 bvalue"
            raise ValueError(e_s)

        ReconstModel.__init__(self, gtab)
        self.split_b_D = split_b_D
        self.split_b_S0 = split_b_S0
        self.bounds = bounds
        self.tol = tol
        self.options = options
        self.method = method

        if SCIPY_LESS_0_17 and self.bounds is not None:
            e_s = "Scipy versions less than 0.17 do not support "
            e_s += "bounds. Please update to Scipy 0.17 to use bounds"
            raise ValueError(e_s)
        else:
            self.bounds = ((0., 0., 0., 0.), (np.inf, .6, 0.1, 0.1))

    @multi_voxel_fit
    def fit(self, data, mask=None):
        """ Fit method of the Ivim model class.

        Parameters
        ----------
        data : array
            The measured signal from one voxel. A multi voxel decorator
            will be applied to this fit method to scale it and apply it
            to multiple voxels.

        mask : array
            A boolean array used to mark the coordinates in the data that
            should be analyzed that has the shape data.shape[:-1]

        Returns
        -------
        IvimFit object

        References
        ----------
        .. [1] Federau, Christian, et al. "Quantitative measurement
                   of brain perfusion with intravoxel incoherent motion
                   MR imaging." Radiology 265.3 (2012): 874-881.
        """
        # Use the function estimate_S0_prime_D to get S0_prime and D.
        S0_prime, D = self.estimate_S0_prime_D(data)
        S0, D_star_prime = self.estimate_S0_D_star_prime(data)
        f_guess = 1 - S0_prime / S0
        x0_guess = np.array([S0, f_guess, D_star_prime, D])
        f, D_star = self.estimate_f_D_star(data, x0_guess)
        # Use Optimize to get ivim_params
        x0 = np.array([S0, f, D_star, D])
        if self.method == 'one_stage':
            data_normalized = data/np.mean(data[self.gtab.b0s_mask])
            x0_guess = x0
            x0_guess[0] = x0[0]/np.mean(data[self.gtab.b0s_mask])
            res = Optimizer(_ivim_error_regularized, x0_guess, args=(self.gtab, data_normalized, x0_guess))
            params_in_mask = res.xopt
            params_in_mask[0] = params_in_mask[0]*np.mean(data[self.gtab.b0s_mask])
            return IvimFit(self, params_in_mask)

        else:
            if self.bounds is None:
                bounds_check = [(0., 0., 0., 0.), (np.inf, 1., 1., 1.)]
            else:
                bounds_check = self.bounds

            x0 = np.where(x0 > bounds_check[0],
                          x0, bounds_check[0])

            x0 = np.where(x0 < bounds_check[1],
                          x0, bounds_check[1])

            params_in_mask = self._leastsq(data, x0)
            return IvimFit(self, params_in_mask)

    def estimate_S0_prime_D(self, data):
        """Estimate S0_prime and D for bvals > split_b_D
        """
        bvals_ge_split = self.gtab.bvals[self.gtab.bvals >= self.split_b_D]
        bvecs_ge_split = self.gtab.bvecs[self.gtab.bvals >= self.split_b_D]
        gtab_ge_split = gradient_table(bvals_ge_split, bvecs_ge_split.T)

        D, neg_log_S0 = np.polyfit(gtab_ge_split.bvals,
                                   -np.log(data[self.gtab.bvals >= self.split_b_D]), 1)
        S0_prime = np.exp(-neg_log_S0)
        return S0_prime, D

    def estimate_S0_D_star_prime(self, data):
        """Estimate S0 and D_star_prime for bvals < split_b_S0
        """
        bvals_le_split = self.gtab.bvals[self.gtab.bvals < self.split_b_S0]
        bvecs_le_split = self.gtab.bvecs[self.gtab.bvals < self.split_b_S0]
        gtab_le_split = gradient_table(bvals_le_split, bvecs_le_split.T)

        D_star_prime, neg_log_S0 = np.polyfit(gtab_le_split.bvals,
                                              -np.log(data[self.gtab.bvals < self.split_b_S0]), 1)

        S0 = np.exp(-neg_log_S0)
        return S0, D_star_prime

    def estimate_f_D_star(self, data, x0):
        """Estimate D_star using the values of all the other parameters obtained before
        """
        gtol = self.options["gtol"]
        ftol = self.options["ftol"]
        xtol = self.tol
        epsfcn = self.options["eps"]
        maxfev = self.options["maxiter"]

        if SCIPY_LESS_0_17:
            res = leastsq(f_D_star_error,
                          [x0[2], 10 * x0[3]],
                          args=(self.gtab, data, x0),
                          gtol=gtol,
                          xtol=xtol,
                          ftol=ftol,
                          epsfcn=epsfcn,
                          maxfev=maxfev)
            f, D_star = res[0]
            return f, D_star
        else:
            res = least_squares(f_D_star_error,
                                [x0[2], 10 * x0[3]],
                                args=(self.gtab, data, x0),
                                ftol=ftol,
                                xtol=xtol,
                                gtol=gtol,
                                bounds=((0., 0.), (1., 1.)),
                                max_nfev=maxfev, )
            f, D_star = res.x
            return f, D_star

    def predict(self, ivim_params, gtab, S0=1.):
        """
        Predict a signal for this IvimModel class instance given parameters.

        Parameters
        ----------
        ivim_params : array
            The ivim parameters as an array [S0, f, D_star and D]

        Returns
        -------
        ivim_signal : array
            The predicted IVIM signal using given parameters.
        """
        return ivim_prediction(ivim_params, gtab)

    def _leastsq(self, data, x0):
        """
        Use leastsq for finding ivim_params

        Parameters
        ----------
        data : array, (len(bvals))
            An array containing the signal from a voxel.
            If the data was a 3D image of 10x10x10 grid with 21 bvalues,
            the multi_voxel decorator will run the single voxel fitting
            on all the 1000 voxels to get the parameters in
            IvimFit.model_paramters. The shape of the parameter array
            will be (data[:-1], 4).

        x0 : array
            Initial guesses for the parameters S0, f, D_star and D
            calculated using the function `estimate_x0`
        """
        gtol = self.options["gtol"]
        ftol = self.options["ftol"]
        xtol = self.tol
        epsfcn = self.options["eps"]
        maxfev = self.options["maxiter"]
        bounds = self.bounds

        if SCIPY_LESS_0_17:
            res = leastsq(_ivim_error,
                          x0,
                          args=(self.gtab, data),
                          gtol=gtol,
                          xtol=xtol,
                          ftol=ftol,
                          epsfcn=epsfcn,
                          maxfev=maxfev)
            ivim_params = res[0]
            return ivim_params
        else:
            res = least_squares(_ivim_error,
                                x0,
                                bounds=bounds,
                                ftol=ftol,
                                xtol=xtol,
                                gtol=gtol,
                                max_nfev=maxfev,
                                args=(self.gtab, data))
            ivim_params = res.x
            return ivim_params


class IvimFit(object):
    def __init__(self, model, model_params):
        """ Initialize a IvimFit class instance.
            Parameters
            ----------
            model : Model class
            model_params : array
                The parameters of the model. In this case it is an
                array of ivim parameters. If the fitting is done
                for multi_voxel data, the multi_voxel decorator will
                run the fitting on all the voxels and model_params
                will be an array of the dimensions (data[:-1], 4),
                i.e., there will be 4 parameters for each of the voxels.
        """
        self.model = model
        self.model_params = model_params

    def __getitem__(self, index):
        model_params = self.model_params
        N = model_params.ndim
        if type(index) is not tuple:
            index = (index,)
        elif len(index) >= model_params.ndim:
            raise IndexError("IndexError: invalid index")
        index = index + (slice(None),) * (N - len(index))
        return type(self)(self.model, model_params[index])

    @property
    def S0_predicted(self):
        return self.model_params[..., 0]

    @property
    def perfusion_fraction(self):
        return self.model_params[..., 1]

    @property
    def D_star(self):
        return self.model_params[..., 2]

    @property
    def D(self):
        return self.model_params[..., 3]

    @property
    def shape(self):
        return self.model_params.shape[:-1]

    def predict(self, gtab, S0=1.):
        r"""
        Given a model fit, predict the signal.

        Parameters
        ----------
        gtab : GradientTable class instance
               Gradient directions and bvalues

        S0 : float
            S0 value here is not necessary and will
            not be used to predict the signal. It has
            been added to conform to the structure
            of the predict method in multi_voxel which
            requires a keyword argument S0.

        Returns
        -------
        signal : array
            The signal values predicted for this model using
            its parameters.
        """
        return ivim_prediction(self.model_params, gtab)