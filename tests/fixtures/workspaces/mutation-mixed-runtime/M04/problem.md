# Mixed Runtime Mutation Fixture

## 题目描述

Consider a zero-indexed grid with $h$ rows and $w$ columns. A cell $(r,c)$ is blocked when $r>0$, $c>0$, and $(37r+61c+s)\bmod p=0$. Starting at $(0,0)$, move between orthogonally adjacent unblocked cells. Output the number of reachable cells.

## 输入格式

The input contains integers $h$, $w$, $p$, and $s$ ($1\le h,w\le 1500$, $2\le p\le 97$, $0\le s<p$).

## 输出格式

Output the number of reachable cells.
