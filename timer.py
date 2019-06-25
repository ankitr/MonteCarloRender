from __future__ import print_function

from timeit import default_timer as timer
import MC

if __name__ == '__main__':
    start = timer()
    print('Starting')
    MC.lunchPacketwithBatch(nPhotonsRequested=1e6)
    end = timer()
    print('Finished, elapsed in %ss' % (end-start))
