'''
preprocessing.py
Series of functions to aid the preprocessing of a dataset of MRR-PRO measurements.

Copyright (C) 2021  Alfonso Ferrone

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
'''

import os
import copy
import glob
import numpy as np
import netCDF4 as nc
from scipy import ndimage
from configparser import ConfigParser
from algo import reconstruction

# --------------------------------------------------------------------------------------------------
# PLOTTING PARAMETERS
'''
To display the intermediate steps of the pre-processing, just set the following flag to "True".
Parameters such as figure size, DPI, and font size can be changed in the "rc parameters" as shown
below.
'''
PLOT_INTERMEDIATE_STEPS = True

if PLOT_INTERMEDIATE_STEPS:
    # matplotlib necessary only if the debugging plots are enabled 
    import matplotlib.pyplot as plt
    # Figure parameters
    plt.rcParams.update({'figure.dpi': 100})
    plt.rc('font', size=11)
    px = 1/plt.rcParams['figure.dpi']

    figsize_2panelS = (800*px, 350*px)

    spectrum_cmap = 'inferno'
    mask_cmap = 'RdYlGn_r'

# --------------------------------------------------------------------------------------------------
# LOADING CONSTANTS AND PARAMETERS
# This section access directly the "config.ini" file.
config_fname = "config.ini"
with open(config_fname) as fp:
    config_object = ConfigParser()
    config_object.read_file(fp)

# Directories (for saving the plots, in case they are generated)
path_info = config_object['PATHS']
dir_npy_out = path_info['dir_npy']

# Debugging parameters
debugging_info = config_object['DEBUGGING_PARAMETERS']
IGNORE_WARNINGS = bool(int(debugging_info['IGNORE_WARNINGS']))
if IGNORE_WARNINGS:
    import warnings
    warnings.filterwarnings("ignore")
# --------------------------------------------------------------------------------------------------


def load_dataset(ini_path, subfolder_structure, verbose=False):
    '''
    Find full path of all available measurements files.

    The function looks into the folder provided in input and looks for netCDF files
    (extension ".nc"). The subfolder structure is taken from the variable subfolder_structure in config.ini
    
    Parameters
    ----------
    ini_path : str
        Full path to the folder at the root of the folder tree containing all measurement files.
    subfolder_structure : str
        structure of the subfolders of the files. Eg. '%Y%m/%Y%m%d/' for the metek default subfolder structure YYYYMM/YYYYMMDD/
    verbose : bool
        Flag to specify whether the number of all files found should be displayed.


    Returns
    -------
    all_files : list
        List of all the full paths to all measurement files

    '''
    parts = subfolder_structure.strip("/").split("/")
    glob_pattern = "/".join(["*"] * len(parts)) + "/*.nc"

    all_files = glob.glob(os.path.join(ini_path, glob_pattern))
    all_files.sort()

    if verbose:
        print('Found %d files in total.' % len(all_files))

    return all_files


def concatenate_all_spectra(all_files, spectrum_varname='spectrum_raw', verbose=False,
                            num_profiles=None):
    '''
    Concatenate the spectra collected over multiple days in a 3D matrix.
    
    Given the list of all files provided in input, the function loops over them, open the netCDF
    files, load the spectrum into a 2D array, and then stack all the arrays along a third dimension.
    
    
    Parameters
    ----------
    all_files : list
        List of all the full paths (as strings) to the MRR files composing the dataset to process
    spectrum_varname : str
        Name of the variable containing the spectra in the input NetCDF
    verbose : bool
        Flag to print the fraction of file processed
    num_profiles : int or None
        Debugging parameter, to decide to process maximum a certain number of files.
        None -> all profiles will be processed

    Returns
    -------
    np.concatenate(spectrum_list) : numpy.array (3D)
        Matrix of spectra (dimensions: time, range, velocity)

    '''
    spectrum_list = []

    for i_f, f in enumerate(all_files[:num_profiles]):
        if not i_f % 10:
            print('%d/%d: %s' % (i_f+1, len(all_files[:num_profiles]), f))

        # Reading variables
        with nc.Dataset(f) as ncfile:
            spectrum = np.array(ncfile.variables[spectrum_varname])
        # Loading shape of previous configuration
        if len(spectrum_list):
            N = spectrum_list[-1].shape[1]
            m = spectrum_list[-1].shape[2]
        else:
            N = spectrum.shape[1]
            m = spectrum.shape[2]
        # We proceed only if same configuration
        if (spectrum.shape[1] == N and spectrum.shape[2] == m):
            spectrum_list.append(spectrum)
        else:
            print('Different configuration, skipping: %d' % i_f)

    return np.concatenate(spectrum_list)


def save_dataset_stats(concatenated_spectra, out_fpath, q=0.5):
    '''
    Compute and saves to file the chosen quantiles of the concatenated spectra.

    The function computes the quantiles (some default values are already provided) of the dataset
    obtained by concatenating all the spectra measured during the field campaign.
    Two quantiles are computed: one used in the definition of the interference mask, and the second
    used for determining the correction at the borders (on the velocity axis) of the spectrum.
    The quantiles are computed along the time dimension.

    Parameters
    ----------
    concatenated_spectra : numpy.array
        Matrix of all the spectra in the dataset (dimensions: time, range, velocity)
    out_fpath : str
        Full path of the ".npz" file (numpy format for multiple arrays) in which the numpy arrays
        containing the quantiles will be saved
    q : float
        Quantile (between 0 and 1) to compute for the interference line removal and
        border correction


    '''
    # Simple check on the input 
    q = float(q)
    if q < 0.:
        q = 0.
    elif q > 1.:
        q = 1.

    # Computing the desired quantiles
    if q == 0.5:
        spectra_q = np.nanmedian(concatenated_spectra, axis=0)
    else:
        spectra_q = np.nanquantile(concatenated_spectra, q=q, axis=0)

    # Saving it with numpy
    np.save(out_fpath, spectra_q)


def interference_mask_and_border_correction(in_fpath, out_fpath_interf, out_fpath_border_corr,
                                            out_fpath_reconstructed_median,
                                            out_fpath_interf_isolated_peaks,
                                            max_gradient_multiplier=3.,
                                            chosen_degree_fit=4, min_len_slice=3,
                                            threhsold_prominence_interference=0.2,
                                            max_fraction_of_nan_at_range=0.5,
                                            num_iterations_interf_mask_dilation=2,
                                            margin_l_bord_corr=3, margin_r_bord_corr=3.):
    '''
    Compute and saves to file the interference mask (3rd version)

    Parameters
    ----------
    in_fpath : str
        Full path of the ".npz" file containing the quantiles of the spectra dataset.
    out_fpath_interf : str
        Full path of the ".npy" file in which the interference mask will be saved.
    out_fpath_border_corr : str
        Full path of the ".npy" file in which the border correction will be saved.
    out_fpath_reconstructed_median : str
        Full path of the ".npy" file in which the reconstructed median profile will be saved.
        This profile is obtained by fitting the upper part of the median profile, excluding
        anomalous peaks (interferences).
    out_fpath_interf_isolated_peaks : str
        Full path of the ".npy" file in which the alternative interference mask, which does not
        mask interferences spanning the whole velocity range, will be saved.
    max_gradient_multiplier : float
        Multiplier for computing the maximum vertical gradient allowed in the section of the
        median profile used for the polynomial fit. The values is multiplied to the median gradient
        of the profile.
        Must be positive, non-zero float value.
    chosen_degree_fit : int
        Degree of the polynomial used for the fit of the accepted section of the median profile.
        The fit provides us with a "smoothed" (no major interferences) version of the profile.
        Must be positive, non-zero integer value.
    min_len_slice : int
        Minimum number of adjacent non-NaN gates. If a non-NaN section is shorter than this value,
        it is excluded from the input of the fit. The parameters is used to avoid including the
        peaks of the interferences, usually flat (not excluded by gradient threshold), in the fit. 
        Must be positive, non-zero integer value.
    threhsold_prominence_interference : float
        Threshold on the minimum prominence of a peak at each range gate, above which interferences
        will be detected.
        Must be positive, non-zero float value.
    max_fraction_of_nan_at_range : float
        Maximum number of couples (r, v) at any range that can be considered interference.
        If the number is higher, that whole range gate is considered affected by interference.
        Must be float value between 0 and 1
    num_iterations_interf_mask_dilation : int
        Number of time that the mask will be dilated (to catch margins of interference lines)
    margin_l_bord_corr : int
        The margin at the left (0m/s) of the spectrum when defining the region to be exluded from
        the identification of "isolated bumps", excluded from the boudary correction computation.
        Only positive values are accepted.
    margin_r_bord_corr : int
        Same as margin_l_bord_corr, but for the right sight (v_ny).
        Only positive values are accepted.
    '''
    # ====================
    # Checking valid input
    # ====================
    # Checking for some basic error that may be done when providing the input to the function.
    # The list of checks is not complete, and providing completely wrong input (e.g. wrong types,
    #  nonsensical values...) will probably result in errors or nonsensical outputs.
    max_gradient_multiplier = float(max_gradient_multiplier)
    if max_gradient_multiplier < 0.:
        max_gradient_multiplier = 0.0001

    chosen_degree_fit = int(chosen_degree_fit)
    if chosen_degree_fit < 1:
        chosen_degree_fit = 1

    min_len_slice = int(min_len_slice)
    if min_len_slice < 1:
        min_len_slice = 1

    threhsold_prominence_interference = float(threhsold_prominence_interference)
    if threhsold_prominence_interference < 0.:
        threhsold_prominence_interference = 0.0001

    max_fraction_of_nan_at_range = float(max_fraction_of_nan_at_range)
    if max_fraction_of_nan_at_range < 0.:
        max_fraction_of_nan_at_range = 0.
    elif max_fraction_of_nan_at_range > 1.:
        max_fraction_of_nan_at_range = 1.

    margin_l_bord_corr = int(margin_l_bord_corr)
    if margin_l_bord_corr < 0:
        margin_l_bord_corr = 0

    margin_r_bord_corr = int(margin_r_bord_corr)
    if margin_r_bord_corr < 0:
        margin_r_bord_corr = 0

    # ===========
    # Preparation
    # ===========
    # Loading correct quantile
    spectra_q = np.load(in_fpath)
    m = spectra_q.shape[1]

    # Computing the median per range gate of the input quantile
    median_line = np.nanmedian(spectra_q, axis=1)
    # And its vertical gradient
    median_line_grad = np.gradient(median_line)

    # Detecting a region in the low gates with the opposit trend (excluded from fit)
    min_r_interf = np.nanmin(np.where(median_line_grad < \
                                             np.nanmedian(median_line_grad[median_line_grad < 0.])))
    
    # looking at the upper (negative gradient) region, we threshold the gradient 
    grad_thesh = -max_gradient_multiplier * np.abs(np.nanmedian(median_line_grad[min_r_interf:])) 
    condition_above_peak = np.logical_and(np.logical_and(median_line_grad > grad_thesh,
                                                         median_line_grad < 0.),
                                          np.arange(median_line_grad.shape[0]) > min_r_interf)

    # Now we select the part of profile used as input for the fit
    accepted_r_raw = np.full(median_line_grad.shape, np.nan)
    accepted_r_raw[condition_above_peak] = median_line[condition_above_peak]
    # Enforcing non-NaN slice size condition
    accepted_r = np.full(median_line_grad.shape, np.nan)
    slice_list = reconstruction.slice_at_nan(accepted_r_raw)
    for [sl, sl_values] in slice_list:
        if sl_values.shape[0] > min_len_slice:  
            accepted_r[sl] = sl_values

    if not np.sum(condition_above_peak):
        print('ERROR: Not enough acceptable range gates for fitting median profile.')
        return [], []

    if PLOT_INTERMEDIATE_STEPS:
        fig, axes = plt.subplots(1, 2, figsize=figsize_2panelS)

        ax1 = axes[0]
        ax2 = axes[1]

        ax1.plot(np.arange(median_line.shape[0]), median_line, label='median_line')
        ax1.plot(np.arange(median_line.shape[0]), accepted_r, ls='-', label='accepted_r')

        ax2.plot(np.arange(median_line.shape[0]), median_line_grad)
        ax2.set_yscale('symlog', linthresh=0.1)
        # The limits of accepted gradient
        ax2.axhline(0., ls=':', c='r')
        ax2.axhline(grad_thesh, ls=':', c='r')

        ax1.set_title('Median')
        ax2.set_title('Abs. gradient of median')

        ax1.legend()

        for ax in axes.flatten():
            ax.grid(ls=':', c='gray', alpha=0.5)
            ax.set_xlabel('Velocity bin index')
            ax.set_ylabel('Range gate index')

        plt.tight_layout()
        plt.savefig(os.path.join(dir_npy_out, 'median_line_and_gradients.png'))

    # Polynomial fit
    # Preparing the inputs
    x = np.arange(median_line.shape[0])[np.isfinite(accepted_r)] 
    y = accepted_r[np.isfinite(accepted_r)]
    # And the x at which we want to predict the profile
    x_fit = np.arange(median_line.shape[0])[min_r_interf:]

    # Computing fit parameters
    r_fit_params = np.polyfit(x, y, chosen_degree_fit, full=False)
    r_fit_function = np.poly1d(r_fit_params)

    # Computing the new "fitted profile"
    fitted_r = np.full(median_line.shape, np.nan)
    fitted_r[min_r_interf:] = r_fit_function(x_fit)

    reconstructed_median_line = np.full(median_line.shape, np.nan)
    reconstructed_median_line[:min_r_interf] = median_line[:min_r_interf]
    reconstructed_median_line[min_r_interf:] = np.min(np.stack([fitted_r, median_line]),
                                                      axis=0)[min_r_interf:]

    if PLOT_INTERMEDIATE_STEPS:
        fig, axes = plt.subplots(1, 2, figsize=figsize_2panelS)

        ax1 = axes[0]
        ax2 = axes[1]

        ax1.plot(np.arange(median_line.shape[0]), median_line, label='median_line')
        ax1.plot(np.arange(median_line.shape[0]), reconstructed_median_line, ls='-', c='tab:red',
                 label='reconstructed_median_line')
        ax1.plot(np.arange(median_line.shape[0]), fitted_r, ls=':', c='tab:green')

        ax2.plot(np.arange(median_line.shape[0]), median_line-reconstructed_median_line)

        ax1.set_ylim((min(np.nanmin(median_line[min_r_interf:]),
                          np.nanmin(reconstructed_median_line[min_r_interf:])) - 0.1,
                      max(np.nanmax(median_line[min_r_interf:]),
                          np.nanmax(reconstructed_median_line[min_r_interf:])) + 0.1))

        ax1.set_title('Median line')
        ax2.set_title('Difference original - reconstructed')

        for ax in axes.flatten():
            ax.grid(ls=':', c='gray', alpha=0.5)
            ax.set_xlabel('Velocity bin index')
            ax.set_ylabel('Range gate index')
            
        plt.tight_layout()
        plt.savefig(os.path.join(dir_npy_out, 'median_line_vs_fit.png'))


    # Computing "anomalies" from reconstructed (fitted) median
    diff_from_median = spectra_q - np.tile(reconstructed_median_line, (m, 1)).T

    if PLOT_INTERMEDIATE_STEPS:
        fig, axes = plt.subplots(1, 2, figsize=figsize_2panelS)

        ax1 = axes[0]
        ax2 = axes[1]
        mappable1 = ax1.pcolormesh(np.arange(m), np.arange(spectra_q.shape[0]), 
                                   spectra_q, cmap=spectrum_cmap)
        plt.sca(ax1)
        plt.colorbar(mappable1)
        ax1.set_title('Median of all')

        mappable2 = ax2.pcolormesh(np.arange(m), np.arange(diff_from_median.shape[0]), 
                                   diff_from_median, cmap=spectrum_cmap)
        plt.sca(ax2)
        plt.colorbar(mappable2)
        ax2.set_title('Diff. from reconstructed median')

        for ax in axes.flatten():
            ax.set_facecolor('gray')
            ax.set_xlabel('Velocity bin index')
            ax.set_ylabel('Range gate index')

        plt.tight_layout()
        plt.savefig(os.path.join(dir_npy_out, 'anomalies.png'))

    # =============================
    # Preliminary interference mask
    # =============================
    # Raw mask, just thresholding
    interf_mask_nonfull = np.zeros(diff_from_median.shape, dtype='bool')
    interf_mask_nonfull[diff_from_median > threhsold_prominence_interference] = True

    # Removing interference lines spanning the whole spectrum
    interf_mask_nonfull[np.sum(interf_mask_nonfull, axis=1) > m - \
                                                          margin_l_bord_corr - \
                                                          margin_r_bord_corr, :] = False
    interf_mask_nonfull[0:margin_l_bord_corr, :] = False
    interf_mask_nonfull[-margin_r_bord_corr:, :] = False

    # Removing remaining "thin lines"
    interf_mask_nonfull = ndimage.binary_erosion(interf_mask_nonfull).astype(interf_mask_nonfull.dtype)
    interf_mask_nonfull = ndimage.binary_dilation(interf_mask_nonfull).astype(interf_mask_nonfull.dtype)

    # Svaing to file
    np.save(out_fpath_interf_isolated_peaks, interf_mask_nonfull)
    
    # =================
    # Border correction
    # =================
    # Masking the quantile loaded
    quantile_for_border_corr = copy.deepcopy(spectra_q)
    quantile_for_border_corr[interf_mask_nonfull] = np.nan

    if PLOT_INTERMEDIATE_STEPS:
        fig, axes = plt.subplots(1, 2, figsize=figsize_2panelS)

        ax1 = axes[0]
        ax2 = axes[1]
        mappable1 = ax1.pcolormesh(np.arange(m), np.arange(interf_mask_nonfull.shape[0]), 
                                   interf_mask_nonfull, cmap=mask_cmap, vmin=0, vmax=1)
        plt.sca(ax1)
        plt.colorbar(mappable1)
        ax1.set_title('Preliminary interference mask')


        mappable2 = ax2.pcolormesh(np.arange(m), np.arange(quantile_for_border_corr.shape[0]), 
                                   quantile_for_border_corr, cmap=spectrum_cmap)
        plt.sca(ax2)
        plt.colorbar(mappable2)
        ax2.set_title('Masked median')

        for ax in axes.flatten():
            ax.set_facecolor('gray')
            ax.set_xlabel('Velocity bin index')
            ax.set_ylabel('Range gate index')

        plt.tight_layout()
        plt.savefig(os.path.join(dir_npy_out, 'preliminary_interf_mask.png'))

    # The new median needs to include the "full line" interferences
    median_line_for_border_corr = np.nanmedian(quantile_for_border_corr, axis=1)

    # Computing the correction
    border_corr = np.tile(median_line_for_border_corr, (m, 1)).T  - quantile_for_border_corr
    border_corr[np.isnan(border_corr)] = 0.
    border_corr[border_corr < 0.] = 0.

    # Imposing a condition on the rare case than multiple "isolated" peaks are at the same
    # range gate
    num_significant_corrections = np.sum(border_corr, axis=1) > 2 * threhsold_prominence_interference 
    border_corr[num_significant_corrections > 6] = 0.

    # Saving it
    np.save(out_fpath_border_corr, border_corr)

    # =================
    # Interference mask
    # =================
    # We want the interference mask to keep into account the border correction, so that we do not
    # underestimate the interferences at the extreme Doppler velocity values
    # -> We have to repeat all the steps from before

    # New "corrected" quantile (at border + masked isolated interference)
    corrected_spectra_q = spectra_q + border_corr
    corrected_spectra_q[interf_mask_nonfull] = np.nan

    # Computing the median per range gate of the input quantile
    corrected_median_line = np.nanmedian(corrected_spectra_q, axis=1)
    # And its vertical gradient
    corrected_median_line_grad = np.gradient(corrected_median_line)

    # Detecting a region in the low gates with the opposit trend (excluded from fit)
    corrected_min_r_interf = np.nanmin(np.where(corrected_median_line_grad < \
                        np.nanmedian(corrected_median_line_grad[corrected_median_line_grad < 0.])))

    
    # Looking at the upper (negative gradient) region, we threshold the gradient 
    corrected_grad_thesh = -max_gradient_multiplier * \
                           np.abs(np.nanmedian(corrected_median_line_grad[corrected_min_r_interf:])) 
    corrected_condition_above_peak = np.logical_and(np.logical_and(\
                                                corrected_median_line_grad > corrected_grad_thesh,
                                                corrected_median_line_grad < 0.),
                                                np.arange(corrected_median_line_grad.shape[0])>\
                                                                             corrected_min_r_interf)

    if not np.sum(corrected_condition_above_peak):
        print('ERROR: Not enough acceptable range gates for fitting median profile after applying' +
              ' the border correction.')
        return [], []

    # Now we select the part of profile used as input for the fit
    corrected_accepted_r_raw = np.full(corrected_median_line_grad.shape, np.nan)
    corrected_accepted_r_raw[corrected_condition_above_peak] = \
                                               corrected_median_line[corrected_condition_above_peak]
    # Enforcing non-NaN slice size condition
    corrected_accepted_r = np.full(corrected_median_line_grad.shape, np.nan)
    corrected_slice_list = reconstruction.slice_at_nan(corrected_accepted_r_raw)
    for [sl, sl_values] in corrected_slice_list:
        if sl_values.shape[0] > min_len_slice:  
            corrected_accepted_r[sl] = sl_values

    if PLOT_INTERMEDIATE_STEPS:
        fig, axes = plt.subplots(1, 2, figsize=figsize_2panelS)

        ax1 = axes[0]
        ax2 = axes[1]

        ax1.plot(np.arange(corrected_median_line.shape[0]), corrected_median_line,
                           label='median_line')
        ax1.plot(np.arange(corrected_median_line.shape[0]), corrected_accepted_r,
                           ls='-', label='accepted_r')

        ax2.plot(np.arange(corrected_median_line.shape[0]), corrected_median_line_grad)
        ax2.set_yscale('symlog', linthresh=0.1)
        # The limits of accepted gradient
        ax2.axhline(0., ls=':', c='r')
        ax2.axhline(grad_thesh, ls=':', c='r')

        ax1.set_title('Corrected median')
        ax2.set_title('Corrected abs. grad. of median')

        ax1.legend()

        for ax in axes.flatten():
            ax.grid(ls=':', c='gray', alpha=0.5)
            ax.set_xlabel('Velocity bin index')
            ax.set_ylabel('Range gate index')

        plt.tight_layout()
        plt.savefig(os.path.join(dir_npy_out, 'corrected_median_line_and_gradients.png'))

    # Polynomial fit
    # Preparing the inputs
    corrected_x = np.arange(corrected_median_line.shape[0])[np.isfinite(corrected_accepted_r)] 
    corrected_y = corrected_accepted_r[np.isfinite(corrected_accepted_r)]
    # And the x at which we want to predict the profile
    corrected_x_fit = np.arange(corrected_median_line.shape[0])[corrected_min_r_interf:]

    # Computing fit parameters
    corrected_r_fit_params = np.polyfit(corrected_x, corrected_y, chosen_degree_fit, full=False)
    corrected_r_fit_function = np.poly1d(corrected_r_fit_params)

    # Computing the new "fitted profile"
    corrected_fitted_r = np.full(corrected_median_line.shape, np.nan)
    corrected_fitted_r[corrected_min_r_interf:] = r_fit_function(corrected_x_fit)

    corrected_reconstructed_median_line = np.full(corrected_median_line.shape, np.nan)
    corrected_reconstructed_median_line[:corrected_min_r_interf] = corrected_median_line[:corrected_min_r_interf]
    corrected_reconstructed_median_line[corrected_min_r_interf:] = np.min(np.stack([corrected_fitted_r,
                                                                          corrected_median_line]),
                                                                axis=0)[corrected_min_r_interf:]

    if PLOT_INTERMEDIATE_STEPS:
        fig, axes = plt.subplots(1, 2, figsize=figsize_2panelS)

        ax1 = axes[0]
        ax2 = axes[1]

        ax1.plot(np.arange(corrected_median_line.shape[0]), corrected_median_line,
                           label='median_line')
        ax1.plot(np.arange(corrected_median_line.shape[0]), corrected_reconstructed_median_line,
                 ls='-', c='tab:red', label='reconstructed_median_line')
        ax1.plot(np.arange(corrected_median_line.shape[0]), corrected_fitted_r,
                 ls=':', c='tab:green')

        ax2.plot(np.arange(corrected_median_line.shape[0]),
                 corrected_median_line - corrected_reconstructed_median_line)

        ax1.set_ylim((min(np.nanmin(corrected_median_line[corrected_min_r_interf:]),
                          np.nanmin(corrected_reconstructed_median_line[corrected_min_r_interf:]))-0.1,
                      max(np.nanmax(corrected_median_line[corrected_min_r_interf:])+0.2,
                          np.nanmax(corrected_reconstructed_median_line[corrected_min_r_interf:]))+0.2))

        ax1.set_title('Corrected median line')
        ax2.set_title('Corrected diff. orig. - reconstructed')

        for ax in axes.flatten():
            ax.grid(ls=':', c='gray', alpha=0.5)
            ax.set_xlabel('Velocity bin index')
            ax.set_ylabel('Range gate index')
            
        plt.tight_layout()
        plt.savefig(os.path.join(dir_npy_out, 'corrected_median_line_vs_fit.png'))


    # Computing the last quantile matrix: only correction at borders, no interf. mask
    last_spectra_q = spectra_q + border_corr

    # Computing "anomalies" from reconstructed (fitted) median
    corrected_diff_from_median = last_spectra_q - np.tile(corrected_reconstructed_median_line, \
                                                          (m, 1)).T

    # Saving it to file (used in processing)
    np.save(out_fpath_reconstructed_median, corrected_reconstructed_median_line)

    if PLOT_INTERMEDIATE_STEPS:
        fig, axes = plt.subplots(1, 2, figsize=figsize_2panelS)

        ax1 = axes[0]
        ax2 = axes[1]
        mappable1 = ax1.pcolormesh(np.arange(m), np.arange(corrected_spectra_q.shape[0]), 
                                   corrected_spectra_q, cmap=spectrum_cmap)
        plt.sca(ax1)
        plt.colorbar(mappable1)
        ax1.set_title('Corrected median of all')

        mappable2 = ax2.pcolormesh(np.arange(m), np.arange(corrected_diff_from_median.shape[0]), 
                                   corrected_diff_from_median, cmap=spectrum_cmap)
        plt.sca(ax2)
        plt.colorbar(mappable2)
        ax2.set_title('Corr. diff. from reconstr. median')

        for ax in axes.flatten():
            ax.set_facecolor('gray')
            ax.set_xlabel('Velocity bin index')
            ax.set_ylabel('Range gate index')

        plt.tight_layout()
        plt.savefig(os.path.join(dir_npy_out, 'corrected_anomalies.png'))

    # Finally we compute the interference mask
    interf_mask = np.zeros(corrected_diff_from_median.shape, dtype='bool')
    interf_mask[corrected_diff_from_median > threhsold_prominence_interference] = True
    # Removing range gates with too many "masked values"
    interf_mask[np.sum(interf_mask, axis=1) > max_fraction_of_nan_at_range * m, :] = True
    # Expanding it with binary dilation
    interference_mask = ndimage.binary_dilation(interf_mask,
                            iterations=num_iterations_interf_mask_dilation).astype(interf_mask.dtype)

    # Taking into account the gates in which the border correction was off
    interference_mask[num_significant_corrections > 6] = True

    # Saving to file
    np.save(out_fpath_interf, interference_mask)

    # And returning
    return interference_mask, border_corr, corrected_reconstructed_median_line, interf_mask_nonfull
