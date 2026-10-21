import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from shared_engine.cli import main
if __name__=="__main__": main([*sys.argv[1:],"--case",str(Path(__file__).resolve().parents[1])])
