"""Acquisition Control Class."""

import time

import numpy as np

from console.pulseq_interpreter.interface_unrolled_sequence import UnrolledSequence
from console.pulseq_interpreter.sequence_provider import SequenceProvider
from console.spcm_control.ddc import apply_ddc
from console.spcm_control.interface_acquisition_parameter import AcquisitionParameter
from console.spcm_control.rx_device import RxCard
from console.spcm_control.tx_device import TxCard
from console.utilities.load_config import get_instances


class AcquistionControl:
    """Acquisition control class.

    The main functionality of the acquisition control is to orchestrate transmit and receive cards using
    ``TxCard`` and ``RxCard`` instances.

    TODO: Implementation of logging mechanism.
    Use two logs: a high level one as lab-book and a detailed one for debugging.
    """

    def __init__(self, configuration_file: str):
        """Construct acquisition control class.

        Create instances of sequence provider, tx and rx card.
        Setup the measurement cards and get parameters required for a measurement.

        Parameters
        ----------
        configuration_file
            Path to configuration yaml file which is used to create measurement card and sequence
            provider instances.
        """
        # Get instances from configuration file
        ctx = get_instances(configuration_file)
        self.seq_provider: SequenceProvider = ctx[0]
        self.tx_card: TxCard = ctx[1]
        self.rx_card: RxCard = ctx[2]

        # Setup the cards
        self.is_setup: bool = False
        if self.tx_card.connect() and self.rx_card.connect():
            print("Setup of measurement cards successful.")
            self.is_setup = True

        # Get the rx sampling rate for DDC
        self.f_spcm = self.rx_card.sample_rate * 1e6
        # Set sequence provider max. amplitude per channel according to values from tx_card
        self.seq_provider.max_amp_per_channel = self.tx_card.max_amplitude

        self.unrolled_sequence: UnrolledSequence | None = None

        # Read only attributes for data and dwell time of downsampled signal
        self._raw: np.ndarray | None = None
        self._sig: np.ndarray | None = None
        self._ref: np.ndarray | None = None

        self._dwell: float | None = None

    def __del__(self):
        """Class destructor disconnecting measurement cards."""
        if self.tx_card:
            self.tx_card.disconnect()
        if self.rx_card:
            self.rx_card.disconnect()
        print("Measurement cards disconnected.")

    @property
    def raw_data(self) -> np.ndarray | None:
        """Get pre-processed raw data acquired by acquisition control, read-only property.

        Dimensions: [averages, phase encoding, readout]

        Returns
        -------
            Numpy array of down-sampled, phase corrected raw data.

        """
        return self._raw

    @property
    def reference_data(self) -> np.ndarray | None:
        """Get reference signal data acquired by acquisition control, read-only property.

        Dimensions: [averages, phase encoding, readout]

        Returns
        -------
            Numpy array of down-sampled reference signal used for phase correction.

        """
        return self._ref

    @property
    def signal_data(self) -> np.ndarray | None:
        """Get signal data acquired by acquisition control, read-only property.

        Dimensions: [averages, phase encoding, readout]

        Returns
        -------
            Numpy array of down-sampled signal data, which has not been phase corrected.

        """
        return self._sig

    @property
    def dwell_time(self) -> float | None:
        """Get dwell time of down-sampled data, read-only property.

        Returns
        -------
            Dwell time of down-sampled signal.
        """
        return self._dwell

    def run(self, sequence: str, parameter: AcquisitionParameter, num_averages: int = 1) -> None:
        """Run an acquisition job.

        Parameters
        ----------
        sequence
            Path to pulseq sequence file.
        parameter
            Set of acquisition parameters which are required for the acquisition.

        Raises
        ------
        RuntimeError
            The measurement cards are not setup properly.
        FileNotFoundError
            Invalid file ending of sequence file.
        """
        if not self.is_setup:
            raise RuntimeError("Measurement cards are not setup.")

        if not sequence.endswith(".seq"):
            raise FileNotFoundError("Invalid sequence file.")

        self.seq_provider.read(sequence)
        # self.seq_provider.set
        sqnc: UnrolledSequence = self.seq_provider.unroll_sequence(
            larmor_freq=parameter.larmor_frequency, b1_scaling=parameter.b1_scaling, fov_scaling=parameter.fov_scaling
        )

        self.unrolled_sequence = sqnc if sqnc else None

        # Define timeout for acquisition process: 5 sec + sequence duration
        timeout = 5 + sqnc.duration

        self._raw = None
        self._ref = None
        self._sig = None

        for k in range(num_averages):
            print(f">> Acquisition {k+1}/{num_averages}")

            # Start masurement card operations
            self.rx_card.start_operation()
            time.sleep(0.5)
            self.tx_card.start_operation(sqnc)

            # Get start time of acquisition
            time_start = time.time()

            while len(self.rx_card.rx_data) < sqnc.adc_count:
                # Delay poll by 10 ms
                time.sleep(0.01)

                if len(self.rx_card.rx_data) >= sqnc.adc_count:
                    # All the data was received, start post processing
                    self.post_processing(self.rx_card.rx_data, parameter)
                    break

                if time.time() - time_start > timeout:
                    # Could not receive all the data before timeout
                    print(
                        f"Acquisition Timeout: Only received {len(self.rx_card.rx_data)}/{sqnc.adc_count} adc events..."
                    )
                    if len(self.rx_card.rx_data) > 0:
                        self.post_processing(self.rx_card.rx_data, parameter)
                    break

            self.tx_card.stop_operation()
            self.rx_card.stop_operation()

        # Dwell time of down sampled signal: 1 / (f_spcm / kernel_size)
        self._dwell = parameter.downsampling_rate / self.f_spcm

    def post_processing(self, data: list[list[np.ndarray]], parameter: AcquisitionParameter) -> None:
        """Perform data post processing.

        Apply the digital downconversion, filtering an downsampling per numpy array in the list of the received data.

        Parameters
        ----------
        data
            Received list of adc data samples
        parameter
            Acquistion parameter to setup the filter

        Returns
        -------
            List of processed data arrays
        """
        kernel_size = int(2 * parameter.downsampling_rate)
        f_0 = parameter.larmor_frequency
        ro_start = int(parameter.adc_samples / 2)

        sig_list: list = []
        ref_list: list = []

        for gate in data:
            # Read raw and reference signal per gate
            _sig = np.array(gate[0]) * self.rx_card.rx_scaling[0]
            _ref = np.array(gate[1]) * self.rx_card.rx_scaling[1]
            # Down-sampling of raw and reference signal
            _sig = apply_ddc(_sig, kernel_size=kernel_size, f_0=f_0, f_spcm=self.f_spcm)
            _ref = apply_ddc(_ref, kernel_size=kernel_size, f_0=f_0, f_spcm=self.f_spcm)
            # Calculate start point of readout for adc truncation
            ro_start = int(_sig.size / 2 - parameter.adc_samples / 2)
            # Truncate raw and reference signal
            sig_list.append(_sig[ro_start : ro_start + parameter.adc_samples])
            ref_list.append(_ref[ro_start : ro_start + parameter.adc_samples])

        # Stack signal and reference data in first dimension (phase encoding dimension)
        sig: np.ndarray = np.stack(sig_list, axis=0)
        ref: np.ndarray = np.stack(ref_list, axis=0)

        # Do the phase correction
        raw: np.ndarray = sig * np.exp(-1j * np.angle(ref))

        # Assign processed data to private class attributes
        # Add average dimension
        self._sig = sig[None, ...] if self._sig is None else np.concatenate((self._sig, sig[None, ...]), axis=0)
        self._ref = ref[None, ...] if self._ref is None else np.concatenate((self._ref, ref[None, ...]), axis=0)
        self._raw = raw[None, ...] if self._raw is None else np.concatenate((self._raw, raw[None, ...]), axis=0)

        # Save the following code for later, linear fit was not the best practice so far
        # # Do a linear fit of the reference phase
        # _time = np.arange(_tmp[1].size) * kernel_size / self.f_spcm
        # m_phase, b_phase = np.polyfit(_time, ref_phase, 1)
        # phase_fit = m_phase * _time + b_phase