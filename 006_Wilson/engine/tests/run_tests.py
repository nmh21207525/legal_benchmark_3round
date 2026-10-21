import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
ROOT=Path(__file__).resolve().parents[3]
if __name__=="__main__":
 result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover(str(ROOT/"shared_engine"),pattern="test*.py",top_level_dir=str(ROOT)))
 sys.exit(not result.wasSuccessful())
