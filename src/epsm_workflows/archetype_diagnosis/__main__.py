"""Run a reproducible focused diagnostic study without modifying its parent."""
import argparse
from pathlib import Path
from threadpoolctl import threadpool_limits
from .audit import load_config, prepare, verify

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage',choices=['prepare','regress','physics','report','run'])
    parser.add_argument('--config',required=True)
    args=parser.parse_args(argv);cfg=load_config(args.config);root=Path(cfg['output'])
    with threadpool_limits(2):
        if args.stage in ('prepare','run'):
            if not (root/'prepare-complete.json').exists():prepare(cfg)
            else:verify(cfg)
        if args.stage in ('regress','run'):
            from .regression import regress
            verify(cfg)
            if not (root/'regression/complete.json').exists():regress(cfg)
        if args.stage in ('physics','run'):
            from .physics import physics
            verify(cfg)
            if not (root/'physics/complete.json').exists():physics(cfg)
        if args.stage in ('report','run'):
            from .report import report
            verify(cfg);print(report(cfg),flush=True)

if __name__=='__main__':main()
