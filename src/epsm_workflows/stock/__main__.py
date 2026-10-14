"""CLI stages for the evidence-led national stock workflow."""
import argparse
from pathlib import Path
from threadpoolctl import threadpool_limits
from .common import load_config

def main(argv=None):
 parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='stage',required=True)
 p=sub.add_parser('prepare');p.add_argument('--config',required=True);p.add_argument('--output',required=True)
 for stage in ['regress','cluster','calibrate','evaluate','report']:
  p=sub.add_parser(stage);p.add_argument('--study',required=True)
  if stage=='calibrate':p.add_argument('--evidence',help='Reviewed physical assumptions and pinned AMY manifest')
 args=parser.parse_args(argv)
 with threadpool_limits(limits=2):
  if args.stage=='prepare':
   from .prepare import prepare
   result=prepare(load_config(args.config),args.output)
  elif args.stage=='regress':
   from .regression import regress
   result=regress(args.study)
  elif args.stage=='cluster':
   from .clustering import cluster
   result=cluster(args.study)
  elif args.stage=='calibrate':
   from .calibration import calibrate
   result=calibrate(args.study,args.evidence)
  elif args.stage=='evaluate':
   from .evaluation import evaluate
   result=evaluate(args.study)
  else:
   from .evaluation import report
   result=report(args.study)
 print(result if isinstance(result,Path) else 'Stage complete')

if __name__=='__main__':main()
