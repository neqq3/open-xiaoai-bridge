"""确定性的人工 PCM；纯整数生成，可与独立 C 程序交换，不含采集音频。"""
import numpy as np


def tone(index):
    positions = np.arange(1024, dtype=np.int64)
    bins = (1, 2, 4, 8, 16, 32, 64, 128)
    left = np.where((positions * bins[index % 8] // 512) % 2 == 0, 1, -1)
    right = np.where((positions * bins[7 - index % 8] // 512) % 2 == 0, 1, -1)
    left *= 500 + index * 421 % 14000
    right *= 100 + index * 631 % 11000
    return np.column_stack((left, right)).astype('<i2').tobytes()


def blocks(case):
    if case == 'cycle':
        block = tone(15)
        for _ in range(5602):
            yield block
        return
    if case != 'mixed':
        raise ValueError(case)
    for i in range(100):
        if i >= 80:
            values = ((np.arange(2048, dtype=np.int64) * 1103515245 + i * 12345) >> 8) & 65535
            yield (values - 32768).astype('<i2').tobytes()
        else:
            yield tone(i)
    for _ in range(1410):
        yield bytes(4096)
    for i in range(80):
        yield tone(79 - i)
