"""One bounded, read-only tablet battery diagnostic; no UI or capture changes."""
import argparse,json,re,subprocess,time
from pathlib import Path


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--target',required=True)
    p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    if args.output.exists():p.error('Choose a new output')
    started=time.time()
    result=subprocess.run(['tools/hdc/hdc.exe','-t',args.target,'shell','hidumper -s 3302 -a -i'],
        capture_output=True,encoding='utf-8',errors='replace',timeout=5)
    if result.returncode or len(result.stdout)>16384 or '[Fail]' in result.stdout:raise ValueError('Battery diagnostic failed')
    allowed={'capacity','batteryLevel','chargingStatus','healthState','pluggedType','voltage','present',
             'nowCurrent','currentAverage','totalEnergy','remainingEnergy','remainingChargeTime','temperature','chargeType'}
    values={name:int(value) for name,value in re.findall(r'^([A-Za-z]+):\s*(-?\d+)\s*$',result.stdout,re.M) if name in allowed}
    if 'capacity' not in values:raise ValueError('Unknown battery diagnostic format')
    record={'sample_pc_interval':[started,time.time()],'source':'BatteryService3302-read-only','device_raw_fields':values,
        'units_converted':False,'capacity_delta_measured':False,'capture_incremental_power_measured':False,
        'note':'One system snapshot; active charging and other workloads prevent attribution to capture.',
        'tablet_ui_actions':[],'screenshots_acquired':0}
    from atomic_output import atomic_bytes
    atomic_bytes(args.output,json.dumps(record,indent=2).encode());print(json.dumps(record,indent=2))


if __name__=='__main__':main()
