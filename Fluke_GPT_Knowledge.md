# Fluke ScopeMeter and Software - Knowledge Source

## Instruments
- Fluke 199C ScopeMeter
- Fluke 19x series protocol compatible

## Communication
- Interface: Serial (OC4USB / IR adapter)
- Default: 1200 baud -> switch to 9600
- Commands:
  - ID = identify scope
  - QW = query waveform
  - RP = replay control
  - RS = recall memory
  - PC = change baud rate

## Channel Mapping
- Channel A = Voltage
- Channel B = Current

## Data Handling
- Binary block transfer (#0 header format)
- Checksum validation required
- Sample parsing:
  - signed/unsigned
  - 1/2/4 byte width
  - grouped samples possible

## Analysis Capabilities
- FFT (windowed)
- THD calculation (up to 15th harmonic)
- Power calculations:
  - Vrms, Irms
  - Real Power (kW)
  - Apparent Power (kVA)
  - Reactive Power (kVAR)
  - Power Factor
  - Phase angle

## Field Use Case
- Generator load bank testing
- AVR stability
- Frequency response under load steps
- Harmonic distortion detection
- Worst-case frame detection

## Output
- Waveform PNG
- FFT PNG
- Frame summary TXT
- CSV summary
- FINAL_GLOBAL_REPORT.txt

## Report Location
C:\Users\JimGr\Desktop\FlukeReplayFinalReports

## Known Constraints
- Replay must be active on scope
- Deep memory not directly accessible
- Optical interface alignment critical
