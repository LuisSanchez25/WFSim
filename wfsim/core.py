import logging

from numba import njit
import numpy as np
from scipy.interpolate import interp1d
from tqdm import tqdm

from .load_resource import load_config
from strax import exporter
from . import units
from .utils import find_intervals_below_threshold

export, __all__ = exporter()
__all__.append('PULSE_TYPE_NAMES')

log = logging.getLogger('SimulationCore')

PULSE_TYPE_NAMES = ('RESERVED', 's1', 's2', 'unknown', 'pi_el', 'pmt_ap', 'pe_el')

@export
class Pulse(object):
    """Pulse building class"""

    def __init__(self, config):
        self.config = config
        self.config.update(getattr(self.config, self.__class__.__name__, {}))
        self.resource = load_config(config)

        self.init_pmt_current_templates()
        self.init_spe_scaling_factor_distributions()
        self.turned_off_pmts = np.arange(len(config['gains']))[np.array(config['gains']) == 0]
        
        self.clear_pulse_cache()


    def __call__(self):
        """
        PMTs' response to incident photons
        Use _photon_timings, _photon_channels to build pulses
        """
        if ('_photon_timings' not in self.__dict__) or \
                ('_photon_channels' not in self.__dict__):
            raise NotImplementedError
        
        # The pulse cache should be immediately transfered after call this function
        self.clear_pulse_cache()

        # Correct for PMT Transition Time Spread (skip for pmt afterpulses)
        if '_photon_gains' not in self.__dict__:
            self._photon_timings += np.random.normal(self.config['pmt_transit_time_mean'],
                                                     self.config['pmt_transit_time_spread'],
                                                     len(self._photon_timings))

        dt = self.config.get('sample_duration', 10) # Getting dt from the lib just once
        self._n_double_pe = self._n_double_pe_bot = 0 # For truth aft output

        counts_start = 0 # Secondary loop index for assigning channel
        for channel, counts in zip(*np.unique(self._photon_channels, return_counts=True)):

            #TODO: This is temporary continue to avoid out-of-range error.
            # It should be added a proper method for nVeto PMTs also.
            if self.config['nv']:
                if 0 > channel or channel >= 120:
                    continue
            else:
                if 0 > channel or channel >= 493:
                    continue
            # Use 'counts' amount of photon for this channel
            _channel_photon_timings = self._photon_timings[counts_start:counts_start+counts]
            counts_start += counts
            if channel in self.turned_off_pmts: continue

            # If gain of each photon is not specifically assigned
            # Sample from spe scaling factor distribution and to individual gain
            # In contrast to pmt afterpulse that should have gain determined before this step
            if '_photon_gains' not in self.__dict__:
                if self.config['detector'] == 'XENON1T':
                    _channel_photon_gains = self.config['gains'][channel] \
                    * self.uniform_to_pe_arr(np.random.random(len(_channel_photon_timings)), channel)

                else:
                    _channel_photon_gains = self.config['gains'][channel] \
                    * self.uniform_to_pe_arr(np.random.random(len(_channel_photon_timings)))

                # Add some double photoelectron emission by adding another sampled gain
                n_double_pe = np.random.binomial(len(_channel_photon_timings),
                                                 p=self.config['p_double_pe_emision'])
                self._n_double_pe += n_double_pe
                if channel in self.config['channels_bottom']:
                    self._n_double_pe_bot += n_double_pe

                #_dpe_index = np.random.randint(len(_channel_photon_timings),
                #                               size=n_double_pe)
                if self.config['detector'] == 'XENON1T':
                    _channel_photon_gains[:n_double_pe] += self.config['gains'][channel] \
                    * self.uniform_to_pe_arr(np.random.random(n_double_pe), channel)
                else:
                    _channel_photon_gains[:n_double_pe] += self.config['gains'][channel] \
                    * self.uniform_to_pe_arr(np.random.random(n_double_pe))
            else:
                _channel_photon_gains = np.array(self._photon_gains[self._photon_channels == channel])

            # Build a simulated waveform, length depends on min and max of photon timings
            min_timing, max_timing = np.min(
                _channel_photon_timings), np.max(_channel_photon_timings)
            pulse_left = int(min_timing // dt) - int(self.config['samples_to_store_before'])
            pulse_right = int(max_timing // dt) + int(self.config['samples_to_store_after'])
            pulse_current = np.zeros(pulse_right - pulse_left + 1)

            Pulse.add_current(_channel_photon_timings.astype(int),
                              _channel_photon_gains,
                              pulse_left,
                              dt,
                              self._pmt_current_templates,
                              pulse_current)

            # For single event, data of pulse level is small enough to store in dataframe
            self._pulses.append(dict(
                photons  = len(_channel_photon_timings),
                channel  = channel,
                left     = pulse_left,
                right    = pulse_right,
                duration = pulse_right - pulse_left + 1,
                current  = pulse_current,))


    def init_pmt_current_templates(self):
        """
        Create spe templates, for 10ns sample duration and 1ns rounding we have:
        _pmt_current_templates[i] : photon timing fall between [10*m+i, 10*m+i+1)
        (i, m are integers)
        """

        # Interpolate on cdf ensures that each spe pulse would sum up to 1 pe*sample duration^-1
        pe_pulse_function = interp1d(
            self.config.get('pe_pulse_ts'),
            np.cumsum(self.config.get('pe_pulse_ys')),
            bounds_error=False, fill_value=(0, 1))

        # Samples are always multiples of sample_duration
        sample_duration = self.config.get('sample_duration', 10)
        samples_before = self.config.get('samples_before_pulse_center', 2)
        samples_after = self.config.get('samples_after_pulse_center', 20)
        pmt_pulse_time_rounding = self.config.get('pmt_pulse_time_rounding', 1.0)

        # Let's fix this, so everything can be turned into int
        assert pmt_pulse_time_rounding == 1

        samples = np.linspace(-samples_before * sample_duration,
                              + samples_after * sample_duration,
                              1 + samples_before + samples_after)
        self._template_length = np.int(len(samples) - 1)

        templates = []
        for r in np.arange(0, sample_duration, pmt_pulse_time_rounding):
            pmt_current = np.diff(pe_pulse_function(samples - r)) / sample_duration  # pe / 10 ns
            # Normalize here to counter tiny rounding error from interpolation
            pmt_current *= (1 / sample_duration) / np.sum(pmt_current)  # pe / 10 ns
            templates.append(pmt_current)
        self._pmt_current_templates = np.array(templates)

        log.debug('Create spe waveform templates with %s ns resolution' % pmt_pulse_time_rounding)


    def init_spe_scaling_factor_distributions(self):
        # Extract the spe pdf from a csv file into a pandas dataframe
        spe_shapes = self.resource.photon_area_distribution

        # Create a converter array from uniform random numbers to SPE gains (one interpolator per channel)
        # Scale the distributions so that they have an SPE mean of 1 and then calculate the cdf
        uniform_to_pe_arr = []
        for ch in spe_shapes.columns[1:]:  # skip the first element which is the 'charge' header
            if spe_shapes[ch].sum() > 0:
                mean_spe = (spe_shapes['charge'].values * spe_shapes[ch]).sum() / spe_shapes[ch].sum()
                scaled_bins = spe_shapes['charge'].values / mean_spe
                cdf = np.cumsum(spe_shapes[ch]) / np.sum(spe_shapes[ch])
            else:
                # if sum is 0, just make some dummy axes to pass to interpolator
                cdf = np.linspace(0, 1, 10)
                scaled_bins = np.zeros_like(cdf)

            grid_cdf = np.linspace(0, 1, 2001)
            grid_scale = interp1d(cdf, scaled_bins, 
                bounds_error=False, 
                fill_value=(scaled_bins[0], scaled_bins[-1]))(grid_cdf)

            uniform_to_pe_arr.append(grid_scale)

        if len(uniform_to_pe_arr):
            self.__uniform_to_pe_arr = np.stack(uniform_to_pe_arr)

        log.debug('Initialize spe scaling factor distributions')


    def uniform_to_pe_arr(self, p, channel=0):
        indices = (p * 2000).astype(int)
        return self.__uniform_to_pe_arr[channel, indices]


    def clear_pulse_cache(self):
        self._pulses = []

    @staticmethod
    @njit
    def add_current(photon_timings,
                    photon_gains,
                    pulse_left,
                    dt,
                    pmt_current_templates,
                    pulse_current):
        #         """
        #         Simulate single channel waveform given the photon timings
        #         photon_timing         - dim-1 integer array of photon timings in unit of ns
        #         photon_gain           - dim-1 float array of ph. 2 el. gain individual photons
        #         pulse_left            - left of the pulse in unit of 10 ns
        #         dt                    - mostly it is 10 ns
        #         pmt_current_templates - list of spe templates of different reminders
        #         pulse_current         - waveform
        #         """
        if not len(photon_timings):
            return
        
        template_length = len(pmt_current_templates[0])
        i_photons = np.argsort(photon_timings)
        # Convert photon_timings to int outside this function
        # photon_timings = photon_timings // 1

        gain_total = 0
        tmp_photon_timing = photon_timings[i_photons[0]]
        for i in i_photons:
            if photon_timings[i] > tmp_photon_timing:
                start = int(tmp_photon_timing // dt) - pulse_left
                reminder = int(tmp_photon_timing % dt)
                pulse_current[start:start + template_length] += \
                    pmt_current_templates[reminder] * gain_total

                gain_total = photon_gains[i]
                tmp_photon_timing = photon_timings[i]
            else:
                gain_total += photon_gains[i]
        else:
            start = int(tmp_photon_timing // dt) - pulse_left
            reminder = int(tmp_photon_timing % dt)
            pulse_current[start:start + template_length] += \
                pmt_current_templates[reminder] * gain_total


    def singlet_triplet_delays(self, size, singlet_ratio):
        """
        Given the amount of the eximer, return time between excimer decay
        and their time of generation.
        size           - amount of eximer
        self.phase     - 'liquid' or 'gas'
        singlet_ratio  - fraction of excimers that become singlets
                         (NOT the ratio of singlets/triplets!)
        """
        if self.phase == 'liquid':
            t1, t3 = (self.config['singlet_lifetime_liquid'],
                      self.config['triplet_lifetime_liquid'])
        elif self.phase == 'gas':
            t1, t3 = (self.config['singlet_lifetime_gas'],
                      self.config['triplet_lifetime_gas'])

        delay = np.random.choice([t1, t3], size, replace=True,
                                 p=[singlet_ratio, 1 - singlet_ratio])
        return np.random.exponential(1, size) * delay


@export
class S1(Pulse):
    """
    Given temperal inputs as well as number of photons
    Random generate photon timing and channel distribution.
    """

    def __init__(self, config):
        super().__init__(config)
        self.phase = 'liquid'  # To distinguish singlet/triplet time delay.

    def __call__(self, instruction):
        if len(instruction.shape) < 1:
            # shape of recarr is a bit strange
            instruction = np.array([instruction])

        _, _, t, x, y, z, n_photons, recoil_type, *rest = [
            np.array(v).reshape(-1) for v in zip(*instruction)]
        
        positions = np.array([x, y, z]).T  # For map interpolation
        ly = np.squeeze(self.resource.s1_light_yield_map(positions),
                       axis=-1)
        if self.config['detector']=='XENON1T':
            ly *= self.config['s1_detection_efficiency']
        n_photons = np.random.binomial(n=n_photons, p=ly)

        self._photon_timings = np.array([])
        list(map(self.photon_timings, t, n_photons, recoil_type))
        # The new way iterpolation is written always require a list
        self.photon_channels(positions, n_photons)

        super().__call__()

    def photon_channels(self, points, n_photons):
        channels = np.array(self.config['channels_in_detector']['tpc'])
        p_per_channel = self.resource.s1_pattern_map(points)
        p_per_channel[:, np.in1d(channels, self.turned_off_pmts)] = 0
        
        self._photon_channels = np.array([]).astype(int)
        for ppc, n in zip(p_per_channel, n_photons):
            self._photon_channels = np.append(self._photon_channels,
                    np.random.choice(
                        channels,
                        size=n,
                        p=ppc / np.sum(ppc),
                        replace=True))

    def photon_timings(self, t, n_photons, recoil_type):
        if n_photons == 0:
            return

        if (self.config.get('s1_model_type') == 'simple' and 
           recoil_type.lower() in ['er', 'nr']):
            # Simple S1 model enabled: use it for ER and NR.
            self._photon_timings = np.append(self._photon_timings,
                t + np.random.exponential(self.config['s1_decay_time'], n_photons))
            return

        try:
            self._photon_timings = np.append(self._photon_timings,
                t + getattr(self, recoil_type.lower())(n_photons))
        except AttributeError:
            raise AttributeError('Recoil type must be ER, NR, alpha or LED, not %s' % recoil_type)

    def alpha(self, size):
        # Neglible recombination time
        return self.singlet_triplet_delays(size, self.config['s1_ER_alpha_singlet_fraction'])

    def led(self, size):
        # distribute photons uniformly within the LED pulse length
        return np.random.uniform(0, self.config['led_pulse_length'], size)

    def er(self, size):
        # How many of these are primary excimers? Others arise through recombination.
        efield = (self.config['drift_field'] / (units.V / units.cm))
        self.config['s1_ER_recombination_time'] = 3.5 / \
                                                  0.18 * (1 / 20 + 0.41) * np.exp(-0.009 * efield)

        reco_time, p_fraction, max_reco_time = (
            self.config['s1_ER_recombination_time'],
            self.config['s1_ER_primary_singlet_fraction'],
            self.config['maximum_recombination_time'])

        timings = np.random.choice([0, reco_time], size, replace=True,
                                   p=[p_fraction, 1 - p_fraction])
        primary = timings == 0
        timings *= 1 / (1 - np.random.uniform(0, 1, size)) - 1
        timings = np.clip(timings, 0, self.config['maximum_recombination_time'])
        size_primary = len(timings[primary])
        timings[primary] += self.singlet_triplet_delays(
            size_primary, self.config['s1_ER_primary_singlet_fraction'])
        timings[~primary] += self.singlet_triplet_delays(
            size - size_primary, self.config['s1_ER_secondary_singlet_fraction'])
        return timings

    def nr(self, size):
        return self.singlet_triplet_delays(size, self.config['s1_NR_singlet_fraction'])


@export
class S2(Pulse):
    """
    Given temperal inputs as well as number of electrons
    Random generate photon timing and channel distribution.
    """

    def __init__(self, config):
        super().__init__(config)

        self.phase = 'gas'  # To distinguish singlet/triplet time delay.
        self.luminescence_switch_threshold = 100  # When to use simplified model (NOT IN USE)

    def __call__(self, instruction):
        if len(instruction.shape) < 1:
            # shape of recarr is a bit strange
            instruction = np.array([instruction])

        _, _, t, x, y, z, n_electron, recoil_type, *rest = [
            np.array(v).reshape(-1) for v in zip(*instruction)]
        
        # Reverse engineerring FDC
        if self.config['field_distortion_on']:
            z_obs, positions = self.inverse_field_distortion(x, y, z)
        else:
            z_obs, positions = z, np.array([x, y]).T

        sc_gain = np.squeeze(self.resource.s2_light_yield_map(positions), axis=-1) \
            * self.config['s2_secondary_sc_gain']

        # Average drift time of the electrons
        self.drift_time_mean = - z_obs / \
            self.config['drift_velocity_liquid'] + self.config['drift_time_gate']

        # Absorb electrons during the drift
        electron_lifetime_correction = np.exp(- 1 * self.drift_time_mean /
            self.config['electron_lifetime_liquid'])
        cy = self.config['electron_extraction_yield'] * electron_lifetime_correction

        #why are there cy greater than 1? We should check this
        cy = np.clip(cy, a_min = 0, a_max = 1)
        n_electron = np.random.binomial(n=n_electron, p=cy)

        # Second generate photon timing and channel
        self.photon_timings(t, n_electron, z_obs, positions, sc_gain)
        self.photon_channels(positions)

        super().__call__()

    def inverse_field_distortion(self, x, y, z):
        positions = np.array([x, y, z]).T
        for i_iter in range(6):  # 6 iterations seems to work
            dr = self.resource.fdc_3d(positions)
            if i_iter > 0: dr = 0.5 * dr + 0.5 * dr_pre  # Average between iter
            dr_pre = dr

            r_obs = np.sqrt(x**2 + y**2) - dr
            x_obs = x * r_obs / (r_obs + dr)
            y_obs = y * r_obs / (r_obs + dr)
            z_obs = - np.sqrt(z**2 + dr**2)
            positions = np.array([x_obs, y_obs, z_obs]).T

        positions = np.array([x_obs, y_obs]).T 
        return z_obs, positions

    def luminescence_timings(self, shape):
        """
        Luminescence time distribution computation
        """
        number_density_gas = self.config['pressure'] / \
                             (units.boltzmannConstant * self.config['temperature'])
        alpha = self.config['gas_drift_velocity_slope'] / number_density_gas

        dG = self.config['elr_gas_gap_length']
        rA = self.config['anode_field_domination_distance']
        rW = self.config['anode_wire_radius']
        dL = self.config['gate_to_anode_distance'] - dG

        VG = self.config['anode_voltage'] / (1 + dL / dG / self.config['lxe_dielectric_constant'])
        E0 = VG / ((dG - rA) / rA + np.log(rA / rW))

        def Efield_r(r): return np.clip(E0 / r, E0 / rA, E0 / rW)

        def velosity_r(r): return alpha * Efield_r(r)

        def Yield_r(r): return Efield_r(r) / (units.kV / units.cm) - \
                               0.8 * self.config['pressure'] / units.bar

        r = np.linspace(dG, rW, 1000)
        dt = - np.diff(r)[0] / velosity_r(r)
        dy = Yield_r(r) / np.sum(Yield_r(r))

        uniform_to_emission_time = interp1d(np.cumsum(dy), np.cumsum(dt),
                                            bounds_error=False, fill_value=(0, sum(dt)))

        probabilities = 1 - np.random.uniform(0, 1, size=shape)
        return uniform_to_emission_time(probabilities)

    def luminescence_timings_garfield(self, xy, shape):
        """
        Luminescence time distribution computation
        """
        assert 's2_luminescence' in self.resource.__dict__, 's2_luminescence model not found'
        assert shape[0] == len(xy), 'Output shape should have same length as positions'

        x_grid, n_grid = np.unique(self.resource.s2_luminescence['x'], return_counts=True)
        i_grid = (n_grid.sum() - np.cumsum(n_grid[::-1]))[::-1]

        tilt = getattr(self.config, 'anode_xaxis_angle', np.pi / 4)
        pitch = getattr(self.config, 'anode_pitch', 0.5)
        rotation_mat = np.array(((np.cos(tilt), -np.sin(tilt)), (np.sin(tilt), np.cos(tilt))))

        jagged = lambda relative_y: (relative_y + pitch / 2) % pitch - pitch / 2
        distance = jagged(np.matmul(xy, rotation_mat)[:, 1])  # shortest distance from any wire

        index = np.zeros(shape).astype(int)
        @njit
        def _luminescence_timings_index(distance, x_grid, n_grid, i_grid, shape, index):
            for ix in range(shape[0]):
                pitch_index = np.argmin(np.abs(distance[ix] - x_grid))
                for iy in range(shape[1]):
                    index[ix, iy] = i_grid[pitch_index] + np.random.randint(n_grid[pitch_index])

        _luminescence_timings_index(distance, x_grid, n_grid, i_grid, shape, index)

        return self.resource.s2_luminescence['t'][index]

    @staticmethod
    @njit
    def electron_timings(t, n_electron, z, sc_gain, timings, gains,
            drift_velocity_liquid,
            drift_time_gate,
            diffusion_constant_liquid,
            electron_trapping_time):
        assert len(timings) == np.sum(n_electron)
        assert len(gains) == np.sum(n_electron)

        i_electron = 0
        for i in np.arange(len(t)):
            # Diffusion model from Sorensen 2011
            drift_time_mean = - z[i] / \
                drift_velocity_liquid + drift_time_gate
            _drift_time_mean = max(drift_time_mean, 0)
            drift_time_stdev = np.sqrt(2 * diffusion_constant_liquid * _drift_time_mean)
            drift_time_stdev /= drift_velocity_liquid
            # Calculate electron arrival times in the ELR region

            for j in np.arange(n_electron[i]):
                _timing = t[i] + \
                    np.random.exponential(electron_trapping_time)
                _timing += np.random.normal(drift_time_mean, drift_time_stdev)
                timings[i_electron] = _timing

                # TODO: add manual fluctuation to sc gain
                gains[i_electron] = sc_gain[i]
                i_electron += 1

    def photon_timings(self, t, n_electron, z, xy, sc_gain):
        # First generate electron timinga
        self._electron_timings = np.zeros(np.sum(n_electron))
        self._electron_gains = np.zeros(np.sum(n_electron))
        _config = [self.config[k] for k in
                   ['drift_velocity_liquid',
                    'drift_time_gate',
                    'diffusion_constant_liquid',
                    'electron_trapping_time']]
        self.electron_timings(t, n_electron, z, sc_gain, 
            self._electron_timings, self._electron_gains, *_config)

        # TODO log this
        if len(self._electron_timings) < 1:
            self._photon_timings = []
            return 1

        # For vectorized calculation, artificially top #photon per electron at +4 sigma
        nele = len(self._electron_timings)
        npho = np.ceil(np.max(self._electron_gains) +
                       4 * np.sqrt(np.max(self._electron_gains))).astype(int)

        if self.config['s2_luminescence_model'] == 'simple':
            self._photon_timings = self.luminescence_timings((nele, npho))
        elif self.config['s2_luminescence_model'] == 'garfield':
            self._photon_timings = self.luminescence_timings_garfield(
                np.repeat(xy, n_electron, axis=0),
                (nele, npho))
        self._photon_timings += np.repeat(self._electron_timings, npho).reshape((nele, npho))

        # Crop number of photons by random number generated with poisson
        probability = np.tile(np.arange(npho), nele).reshape((nele, npho))
        threshold = np.repeat(np.random.poisson(self._electron_gains), npho).reshape((nele, npho))
        self._photon_timings = self._photon_timings[probability < threshold]

        # Special index for match photon to original electron poistion
        self._instruction = np.repeat(
            np.repeat(np.arange(len(t)), n_electron), npho).reshape((nele, npho))
        self._instruction = self._instruction[probability < threshold]

        self._photon_timings += self.singlet_triplet_delays(
            len(self._photon_timings), self.config['singlet_fraction_gas'])

        # The timings generated is NOT randomly ordered, must do shuffle
        # Shuffle within each given n_electron[i]
        # We can do this by first finding out cumulative sum of the photons
        cumulate_npho = np.pad(np.cumsum(threshold[:, 0]), [1, 0])[np.cumsum(n_electron)]
        for i in range(len(cumulate_npho)):
            if i == 0:
                s = slice(0, cumulate_npho[i])
            else:
                s = slice(cumulate_npho[i-1], cumulate_npho[i])
            np.random.shuffle(self._photon_timings[s])

    def photon_channels(self, points):
        # TODO log this
        if len(self._photon_timings) == 0:
            self._photon_channels = []
            return 1
        
        aft = self.config['s2_mean_area_fraction_top']
        aft_random = self.config.get('randomize_fraction_of_s2_top_array_photons', 0)
        channels = np.array(self.config['channels_in_detector']['tpc']).astype(int)
        top_index = np.array(self.config['channels_top'])
        bottom_index = np.array(self.config['channels_bottom'])

        pattern = self.resource.s2_pattern_map(points)  # [position, pmt]
        if pattern.shape[1] - 1 not in bottom_index:
            pattern = np.pad(pattern, [[0, 0], [0, len(bottom_index)]], 
                'constant', constant_values=1)
        sum_pat = np.sum(pattern, axis=1).reshape(-1, 1)
        pattern = np.divide(pattern, sum_pat, out=np.zeros_like(pattern), where=sum_pat!=0)

        assert pattern.shape[0] == len(points)
        assert pattern.shape[1] == len(channels)

        self._photon_channels = np.array([], dtype=int)
        # Randomly assign to channel given probability of each channel
        for unique_i, count in zip(*np.unique(self._instruction, return_counts=True)):
            pat = pattern[unique_i]  # [pmt]

            if aft > 0:  # Redistribute pattern with user specified aft
                _aft = aft * (1 + np.random.normal(0, aft_random))
                _aft = np.clip(_aft, 0, 1)
                pat[top_index] = pat[top_index] / pat[top_index].sum() * _aft
                pat[bottom_index] = pat[bottom_index] / pat[bottom_index].sum() *  (1 - _aft)

            if np.isnan(pat).sum() > 0:  # Pattern map return zeros
                _photon_channels = np.array([-1] * count)
            else:
                _photon_channels = np.random.choice(
                    channels,
                    size=count,
                    p=pat,
                    replace=True)

            self._photon_channels = np.append(self._photon_channels, _photon_channels)

        # Remove photon with channel -1
        mask = self._photon_channels != -1
        self._photon_channels = self._photon_channels[mask]
        self._photon_timings = self._photon_timings[mask]


@export
class PhotoIonization_Electron(S2):
    """
    Produce electron after pulse simulation, using already built cdfs
    The cdfs follow distribution parameters extracted from data.
    """

    def __init__(self, config):
        super().__init__(config)
        self._photon_timings = []

    def generate_instruction(self, signal_pulse, signal_pulse_instruction):
        if len(signal_pulse._photon_timings) == 0: return []
        return self.electron_afterpulse(signal_pulse, signal_pulse_instruction)

    def electron_afterpulse(self, signal_pulse, signal_pulse_instruction):
        """
        For electron afterpulses we assume a uniform x, y
        """
        delaytime_pmf_hist = self.resource.uniform_to_ele_ap

        # To save calculation we first find out how many photon will give rise ap
        n_electron = np.random.poisson(delaytime_pmf_hist.n
                                       * len(signal_pulse._photon_timings)
                                       * self.config['photoionization_modifier'])

        ap_delay = delaytime_pmf_hist.get_random(n_electron).clip(
            self.config['drift_time_gate'] + 1, None)

        # Randomly select original photon as time zeros
        t_zeros = signal_pulse._photon_timings[np.random.randint(
            low=0, high=len(signal_pulse._photon_timings),
            size=n_electron)]

        instruction = np.repeat(signal_pulse_instruction[0], n_electron)

        instruction['type'] = 4 # pi_el
        instruction['time'] = t_zeros
        instruction['x'], instruction['y'] = self._rand_position(n_electron)
        instruction['z'] = - (ap_delay - self.config['drift_time_gate']) * \
            self.config['drift_velocity_liquid']
        instruction['amp'] = 1

        return instruction

    def _rand_position(self, n):
        Rupper = 46

        r = np.sqrt(np.random.uniform(0, Rupper*Rupper, n))
        angle = np.random.uniform(-np.pi, np.pi, n)

        return r * np.cos(angle), r * np.sin(angle)


@export
class PhotoElectric_Electron(S2):
    """
    Produce electron after S2 pulse simulation, using a gaussian distribution
    """

    def __init__(self, config):
        super().__init__(config)
        self._photon_timings = []

    def generate_instruction(self, signal_pulse, signal_pulse_instruction):
        if len(signal_pulse._photon_timings) == 0: return []
        return self.electron_afterpulse(signal_pulse, signal_pulse_instruction)

    def electron_afterpulse(self, signal_pulse, signal_pulse_instruction):

        n_electron = np.random.poisson(self.config['photoelectric_p']
                                       * len(signal_pulse._photon_timings)
                                       * self.config['photoelectric_modifier'])

        ap_delay = np.clip(
            np.random.normal(self.config['photoelectric_t_center'] + self.config['drift_time_gate'], 
                             self.config['photoelectric_t_spread'],
                             n_electron), 0, None)

        # Randomly select original photon as time zeros
        t_zeros = signal_pulse._photon_timings[np.random.randint(
            low=0, high=len(signal_pulse._photon_timings),
            size=n_electron)]

        instruction = np.repeat(signal_pulse_instruction[0], n_electron)

        instruction['type'] = 6 # pe_el
        instruction['time'] = t_zeros
        instruction['x'], instruction['y'] = self._rand_position(n_electron)
        instruction['z'] = - (ap_delay - self.config['drift_time_gate']) * \
            self.config['drift_velocity_liquid']
        instruction['amp'] = 1

        return instruction

    def _rand_position(self, n):
        Rupper = 46

        r = np.sqrt(np.random.uniform(0, Rupper*Rupper, n))
        angle = np.random.uniform(-np.pi, np.pi, n)

        return r * np.cos(angle), r * np.sin(angle)


@export
class PMT_Afterpulse(Pulse):
    """
    Produce pmt after pulse simulation, using already built cdfs
    The cdfs follow distribution parameters extracted from data.
    """

    def __init__(self, config):
        super().__init__(config)

    def __call__(self, signal_pulse):
        if len(signal_pulse._photon_timings) == 0:
            self.clear_pulse_cache()
            return

        self._photon_timings = []
        self._photon_channels = []
        self._photon_amplitude = []

        self.photon_afterpulse(signal_pulse)
        super().__call__()

    def photon_afterpulse(self, signal_pulse):
        """
        For pmt afterpulses, gain and dpe generation is a bit different from standard photons
        """
        self.element_list = self.resource.uniform_to_pmt_ap.keys()
        for element in self.element_list:
            delaytime_cdf = self.resource.uniform_to_pmt_ap[element]['delaytime_cdf']
            amplitude_cdf = self.resource.uniform_to_pmt_ap[element]['amplitude_cdf']

            # Assign each photon FRIST random uniform number rU0 from (0, 1] for timing
            rU0 = 1 - np.random.rand(len(signal_pulse._photon_timings))

            # Select those photons with U <= max of cdf of specific channel
            cdf_max = delaytime_cdf[signal_pulse._photon_channels, -1]
            sel_photon_id = np.where(rU0 <= cdf_max * self.config['pmt_ap_modifier'])[0]
            if len(sel_photon_id) == 0: continue
            sel_photon_channel = signal_pulse._photon_channels[sel_photon_id]

            # Assign selected photon SECOND random uniform number rU1 from (0, 1] for amplitude
            rU1 = 1 - np.random.rand(len(sel_photon_id))

            # The map is made so that the indices are delay time in unit of ns
            if 'Uniform' in element:
                ap_delay = np.random.uniform(delaytime_cdf[sel_photon_channel, 0], 
                    delaytime_cdf[sel_photon_channel, 1])                
                ap_amplitude = np.ones_like(ap_delay)
            else:
                ap_delay = (np.argmin(
                    np.abs(
                        delaytime_cdf[sel_photon_channel]
                        - rU0[sel_photon_id][:, None]), axis=-1)
                            - self.config['pmt_ap_t_modifier'])
                ap_amplitude = np.argmin(
                    np.abs(
                        amplitude_cdf[sel_photon_channel]
                        - rU1[:, None]), axis=-1)/100.

            self._photon_timings += (signal_pulse._photon_timings[sel_photon_id] + ap_delay).tolist()
            self._photon_channels += signal_pulse._photon_channels[sel_photon_id].tolist()
            self._photon_amplitude += np.atleast_1d(ap_amplitude).tolist()

        self._photon_timings = np.array(self._photon_timings)
        self._photon_channels = np.array(self._photon_channels).astype(int)
        self._photon_amplitude = np.array(self._photon_amplitude)
        self._photon_gain = np.array(self.config['gains'])[self._photon_channels] \
            * self._photon_amplitude


@export
class RawData(object):

    def __init__(self, config):
        self.config = config
        self.pulses = dict(
            s1=S1(config),
            s2=S2(config),
            pi_el=PhotoIonization_Electron(config),
            pe_el=PhotoElectric_Electron(config),
            pmt_ap=PMT_Afterpulse(config),
        )
        self.resource = load_config(self.config)

    def __call__(self, instructions, truth_buffer=None):
        if truth_buffer is None:
            truth_buffer = []

        # Pre-load some constents from config
        v = self.config['drift_velocity_liquid']
        rext = self.config['right_raw_extension']

        # Data cache
        self._pulses_cache = []
        self._raw_data_cache = []

        # Iteration conditions
        self.source_finished = False
        self.last_pulse_end_time = - np.inf
        self.instruction_event_number = np.min(instructions['event_number'])

        # Primary instructions must be sorted by signal time
        # int(type) by design S1-esque being odd, S2-esque being even
        # thus type%2-1 is 0:S1-esque;  -1:S2-esque
        # Make a list of clusters of instructions, with gap smaller then rext
        inst_time = instructions['time'] + instructions['z']  / v * (instructions['type'] % 2 - 1)
        inst_queue = np.argsort(inst_time)
        inst_queue = np.split(inst_queue, np.where(np.diff(inst_time[inst_queue]) > rext)[0]+1)

        # Instruction buffer
        instb = np.zeros(100000, dtype=instructions.dtype) # size ~ 1% of size of primary
        instb_filled = np.zeros_like(instb, dtype=bool) # Mask of where buffer is filled

        # ik those are illegible, messy logic. lmk if you have a better way
        pbar = tqdm(total=len(inst_queue), desc='Simulating Raw Records')
        while not self.source_finished:

            # A) Add a new instruction into buffer
            try:
                ixs = inst_queue.pop(0) # The index from original instruction list
                self.source_finished = len(inst_queue) == 0
                assert len(np.where(~instb_filled)[0]) > len(ixs), "Run out of instruction buffer"
                ib = np.where(~instb_filled)[0][:len(ixs)] # The index of first empty slot in buffer
                instb[ib] = instructions[ixs]
                instb_filled[ib] = True
                pbar.update(1)
            except: pass

            # B) Cluster instructions again with gap size <= rext
            instb_indx = np.where(instb_filled)[0]
            instb_type = instb[instb_indx]['type']
            instb_time = instb[instb_indx]['time'] + instb[instb_indx]['z']  \
                / v * (instb_type % 2 - 1)
            instb_queue = np.argsort(instb_time,  kind='stable')
            instb_queue = np.split(instb_queue, 
                np.where(np.diff(instb_time[instb_queue]) > rext)[0]+1)
            
            # C) Push pulse cache out first if nothing comes right after them
            if np.min(instb_time) - self.last_pulse_end_time > rext and not np.isinf(self.last_pulse_end_time):
                self.digitize_pulse_cache()
                yield from self.ZLE()

            # D) Run all clusters before the current source
            stop_at_this_group = False
            for ibqs in instb_queue:
                for ptype in [1, 2, 4, 6]: # S1 S2 PI Gate
                    mask = instb_type[ibqs] == ptype
                    if np.sum(mask) == 0: continue # No such instruction type
                    instb_run = instb_indx[ibqs[mask]] # Take hold of todo list

                    if self.symtype(ptype) in ['s1', 's2']:
                        stop_at_this_group = True # Stop group iteration
                        _instb_run = np.array_split(instb_run, len(instb_run))
                    else: _instb_run = [instb_run] # Small trick to make truth small

                    # Run pulse simulation for real
                    for instb_run in _instb_run:
                        for instb_secondary in self.sim_data(instb[instb_run]):
                            ib = np.where(~instb_filled)[0][:len(instb_secondary)]
                            instb[ib] = instb_secondary
                            instb_filled[ib] = True

                        if len(truth_buffer): # Extract truth info
                            self.get_truth(instb[instb_run], truth_buffer)

                        instb_filled[instb_run] = False # Free buffer AFTER copyting into truth buffer

                if stop_at_this_group: break
                self.digitize_pulse_cache() # from pulse cache to raw data
                yield from self.ZLE()
                
            self.source_finished = len(inst_queue) == 0 and np.sum(instb_filled) == 0
        pbar.close()

    @staticmethod
    def symtype(ptype):
        return PULSE_TYPE_NAMES[ptype]

    def sim_data(self, instruction):
        """Simulate a pulse according to instruction, and yield any additional instructions
        for secondary electron afterpulses.
        """
        # Any additional fields in instruction correspond to temporary
        # configuration overrides. No need to restore the old config:
        # next instruction cannot but contain the same fields.
        if len(instruction.dtype.names) > 8:
            for par in instruction.dtype.names:
                if par in self.config:
                    self.config[par] = instruction[par][0]

        # Simulate the primary pulse
        primary_pulse = self.symtype(instruction['type'][0])
        self.pulses[primary_pulse](instruction)

        # Add PMT afterpulses, if requested
        do_pmt_ap = self.config.get('enable_pmt_afterpulses', True)
        if do_pmt_ap:
            self.pulses['pmt_ap'](self.pulses[primary_pulse])

        # Append pulses we just simulated to our cache
        for pt in [primary_pulse, 'pmt_ap']:
            if pt == 'pmt_ap' and not do_pmt_ap:
                continue

            _pulses = getattr(self.pulses[pt], '_pulses')
            if len(_pulses) > 0:
                self._pulses_cache += _pulses
                self.last_pulse_end_time = max(
                    self.last_pulse_end_time,
                    np.max([p['right'] for p in _pulses]) * 10)

        # Make new instructions for electron afterpulses, if requested
        if primary_pulse in ['s1', 's2']:
            if self.config.get('enable_electron_afterpulses', True):
                yield self.pulses['pi_el'].generate_instruction(
                    self.pulses[primary_pulse], instruction)
                if primary_pulse in ['s2']: # Only add gate ap to s2
                    yield self.pulses['pe_el'].generate_instruction(
                        self.pulses[primary_pulse], instruction)
            self.instruction_event_number = instruction['event_number'][0]
        
    def digitize_pulse_cache(self):
        """
        Superimpose pulses (wfsim definition) into WFs w/ dynamic range truncation
        """
        if len(self._pulses_cache) == 0:
            self._raw_data = []
        else:
            self.current_2_adc = self.config['pmt_circuit_load_resistor'] \
                * self.config['external_amplification'] \
                / (self.config['digitizer_voltage_range'] / 2 ** (self.config['digitizer_bits']))

            self.left = np.min([p['left'] for p in self._pulses_cache]) - self.config['trigger_window']
            self.right = np.max([p['right'] for p in self._pulses_cache]) + self.config['trigger_window']
            assert self.right - self.left < 200000, "Pulse cache too long"

            if self.left % 2 != 0: self.left -= 1 # Seems like a digizier effect


            self._raw_data = np.zeros((801,
                self.right - self.left + 1), dtype=('<i8'))
                                                 
            # Use this mask to by pass non-activated channels
            # Set to true when working with real noise
            self._channel_mask = np.zeros(801, dtype=[('mask', '?'), ('left', 'i8'), ('right', 'i8')])
            self._channel_mask['left'] = int(2**63-1)

            for ix, _pulse in enumerate(self._pulses_cache):
                ch = _pulse['channel']
                self._channel_mask['mask'][ch] = True
                self._channel_mask['left'][ch] = min(_pulse['left'], self._channel_mask['left'][ch])
                self._channel_mask['right'][ch] = max(_pulse['right'], self._channel_mask['right'][ch])
                adc_wave = - np.trunc(_pulse['current'] * self.current_2_adc).astype(int)
                _slice = slice(_pulse['left'] - self.left, _pulse['right'] - self.left + 1)
                
                self._raw_data[ch, _slice] += adc_wave

                if self.config['detector'] == 'XENONnT':
                    adc_wave_he = adc_wave * int(self.config['high_energy_deamplification_factor'])
                    if ch <= self.config['channels_top'][-1]:
                        ch_he = self.config['channels_top_high_energy'][ch]
                        self._raw_data[ch_he, _slice] += adc_wave_he
                        self._channel_mask[ch_he] = True
                        self._channel_mask['left'][ch_he] = self._channel_mask['left'][ch]
                        self._channel_mask['right'][ch_he] = self._channel_mask['right'][ch]
                    elif ch <= self.config['channels_bottom'][-1]:
                        self.sum_signal(adc_wave_he,
                            _pulse['left'] - self.left,
                            _pulse['right'] - self.left + 1,
                            self._raw_data[self.config['channels_in_detector']['sum_signal']])

            self._pulses_cache = []

            self._channel_mask['left'] -= self.left + self.config['trigger_window']
            self._channel_mask['right'] -= self.left - self.config['trigger_window']
            
            
            # Adding noise, baseline and digitizer saturation
            self.add_noise(data=self._raw_data,
                           channel_mask=self._channel_mask,
                           noise_data=self.resource.noise_data,
                           noise_data_length=len(self.resource.noise_data))
            self.add_baseline(self._raw_data, self._channel_mask, 
                self.config['digitizer_reference_baseline'],)
            self.digitizer_saturation(self._raw_data, self._channel_mask)


    def ZLE(self):
        """
        Modified software zero lengh encoding, coverting WFs into pulses (XENON definition)
        """
        # Ask for memory allocation just once
        if 'zle_intervals_buffer' not in self.__dict__:
            self.zle_intervals_buffer = -1 * np.ones((50000, 2), dtype=np.int64)

        for ix, data in enumerate(self._raw_data):
            if not self._channel_mask['mask'][ix]:
                continue
            channel_left, channel_right = self._channel_mask['left'][ix], self._channel_mask['right'][ix]
            data = data[channel_left:channel_right+1]

            # For simulated data taking reference baseline as baseline
            # Operating directly on digitized downward waveform        
            if str(ix) in self.config.get('special_thresholds', {}):
                threshold = self.config['digitizer_reference_baseline'] \
                    - self.config['special_thresholds'][str(ix)] - 1
            else:
                threshold = self.config['digitizer_reference_baseline'] - self.config['zle_threshold'] - 1

            n_itvs_found = find_intervals_below_threshold(
                data,
                threshold=threshold,
                holdoff=self.config['trigger_window'] + self.config['trigger_window'] + 1,
                result_buffer=self.zle_intervals_buffer,)

            itvs_to_encode = self.zle_intervals_buffer[:n_itvs_found]
            itvs_to_encode[:, 0] -= self.config['trigger_window']
            itvs_to_encode[:, 1] += self.config['trigger_window']
            itvs_to_encode = np.clip(itvs_to_encode, 0, len(data) - 1)
            # Land trigger window on even numbers
            itvs_to_encode[:, 0] = np.ceil(itvs_to_encode[:, 0] / 2.0) * 2
            itvs_to_encode[:, 1] = np.floor(itvs_to_encode[:, 1] / 2.0) * 2

            for itv in itvs_to_encode:
                yield ix, self.left + channel_left + itv[0], self.left + channel_left + itv[1], data[itv[0]:itv[1]+1]

    def get_truth(self, instruction, truth_buffer):
        """Write truth in the first empty row of truth_buffer

        :param instruction: Array of instructions that were simulated as a
        single cluster, and should thus get one line in the truth info.
        :param truth_buffer: Truth buffer to write in.
        """
        ix = np.argmin(truth_buffer['fill'])
        tb = truth_buffer[ix]
        peak_type = self.symtype(instruction['type'][0])
        pulse = self.pulses[peak_type]

        for quantum in 'photon', 'electron':
            times = getattr(pulse, f'_{quantum}_timings', [])
            if len(times):
                tb[f'n_{quantum}'] = len(times)
                tb[f't_mean_{quantum}'] = np.mean(times)
                tb[f't_first_{quantum}'] = np.min(times)
                tb[f't_last_{quantum}'] = np.max(times)
                tb[f't_sigma_{quantum}'] = np.std(times)
            else:
                # Peak does not have photons / electrons
                # zero-photon afterpulses can be removed from truth info
                if peak_type not in ['s1', 's2'] and quantum == 'photon':
                    return
                tb[f'n_{quantum}'] = 0
                tb[f't_mean_{quantum}'] = np.nan
                tb[f't_first_{quantum}'] = np.nan
                tb[f't_last_{quantum}'] = np.nan
                tb[f't_sigma_{quantum}'] = np.nan
        
        tb['endtime'] = np.mean(instruction['time']) if np.isnan(tb['t_last_photon']) else tb['t_last_photon']
        channels = getattr(pulse, '_photon_channels', [])
        if self.config.get('exclude_dpe_in_truth', False):
            n_dpe = n_dpe_bot = 0
        else:
            n_dpe = getattr(pulse, '_n_double_pe', 0)
            n_dpe_bot = getattr(pulse, '_n_double_pe_bot', 0)
        tb['n_photon'] += n_dpe
        tb['n_photon'] -= np.sum(np.isin(channels, getattr(pulse, 'turned_off_pmts', [])))

        channels_bottom = list(
            set(self.config['channels_bottom']).difference(getattr(pulse, 'turned_off_pmts', [])))
        tb['n_photon_bottom'] = (
            np.sum(np.isin(channels, channels_bottom))
            + n_dpe_bot)

        # Summarize the instruction cluster in one row of the truth file
        for field in instruction.dtype.names:
            value = instruction[field]
            if len(instruction) > 1 and field in 'txyz':
                tb[field] = np.mean(value)
            elif len(instruction) > 1 and field == 'amp':
                tb[field] = np.sum(value)
            else:
                # Cannot summarize intelligently: just take the first value
                tb[field] = value[0]

        # Signal this row is now filled, so it won't be overwritten
        tb['fill'] = True

    @staticmethod
    @njit
    def sum_signal(adc_wave, left, right, sum_template):
        sum_template[left:right] += adc_wave
        return sum_template

    @staticmethod
    @njit
    def add_noise(data, channel_mask, noise_data, noise_data_length):
        """
        Get chunk(s) of noise sample from real noise data
        """
        for ch in range(data.shape[0]):
            if not channel_mask['mask'][ch]:
                continue
            left, right = channel_mask['left'][ch], channel_mask['right'][ch]
            id_t = np.random.randint(low=0, high=noise_data_length-right+left)
            for ix in range(left, right+1):
                if id_t+ix >= noise_data_length or ix >= len(data[ch]):
                    # Don't create value-errors
                    continue
                data[ch, ix] += noise_data[id_t+ix]

    @staticmethod
    @njit
    def add_baseline(data, channel_mask, baseline):
        for ch in range(data.shape[0]):
            if not channel_mask['mask'][ch]:
                continue
            left, right = channel_mask['left'][ch], channel_mask['right'][ch]
            for ix in range(left, right+1):
                data[ch, ix] += baseline

    @staticmethod
    @njit
    def digitizer_saturation(data, channel_mask):
        for ch in range(data.shape[0]):
            if not channel_mask['mask'][ch]:
                continue
            left, right = channel_mask['left'][ch], channel_mask['right'][ch]
            for ix in range(left, right+1):
                if data[ch, ix] < 0:
                    data[ch, ix] = 0
