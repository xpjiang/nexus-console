"""Experiment to acquire a spin echo spectrum."""

import logging

import matplotlib.pyplot as plt
import numpy as np

import console.utilities.sequences as sequences
from console.spcm_control.acquisition_control import AcquistionControl
from console.spcm_control.interface_acquisition_data import AcquisitionData
from console.spcm_control.interface_acquisition_parameter import AcquisitionParameter

# Create acquisition control instance
acq = AcquistionControl(
    configuration_file="../device_config.yaml",
    console_log_level=logging.INFO,
    file_log_level=logging.DEBUG
)

# Construct a spin echo based spectrum sequence
seq = sequences.se_spectrum.constructor(
    echo_time=20e-3,
    rf_duration=200e-6,
    use_sinc=False
)

# Define acquisition parameters
params = AcquisitionParameter(
    larmor_frequency=2.0395e6,
    decimation=400,
)

# Run the acquisition
acq_data: AcquisitionData = acq.run(parameter=params, sequence=seq)

# Get decimated data from acquisition data object
data = acq_data.raw.squeeze()


# Calculate FFT
data_fft = np.fft.fftshift(np.fft.fft(np.fft.fftshift(data)))
fft_freq = np.fft.fftshift(np.fft.fftfreq(data.size, acq_data.dwell_time))

# Plot spectrum
fig, ax = plt.subplots(1, 1, figsize=(10, 5))
ax.plot(fft_freq, np.abs(data_fft))
ax.set_ylabel("Abs. FFT Spectrum [a.u.]")
ax.set_xlabel("Frequency [Hz]")

# Add information to the acquisition data
acq_data.add_info({
    "note": "Example spin echo spectrum experiment"
})

# Write acquisition data object
acq_data.write(save_unprocessed=False)

# Delete the acquisition control, which disconnects from the measurement cards
del acq
