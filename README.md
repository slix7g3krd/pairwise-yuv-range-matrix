# pairwise-yuv-range-matrix

## 领域

游戏、图形与多媒体

## 主题

YUV 范围与色彩矩阵

## 说明

实际range、矩阵缺省与SEI回写。

初始状态下 `media/yuv_range_matrix.py` 中所有函数抛出 `NotImplementedError`，测试全部失败。
修复实现后运行：

```bash
python -m pytest -q
```

期望全部通过。
