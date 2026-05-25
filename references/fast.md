# inmaker
请在 _函数 里自由发挥，cout 输入数据。
```cpp
#include<bits/stdc++.h>
#define int long long
using namespace std;
void _(int id)
{
    mt19937 rnd(time(0) + id);  //int类型随机数，long long 可用mt19937_64
    
    if(id == 1 || id == 2)
    {
        
    }
    else
    {
        
    }
}

string ss;
signed main()
{
    for(int i = 1; i <= 20; i ++)
    {
        ss = "";
        int ssum = i;
        while(ssum)
        {
            ss += (ssum % 10) ^ 48;
            ssum /= 10;
        }
        reverse(ss.begin(), ss.end());
        freopen((ss + ".in").c_str(), "w", stdout);
        _(i);
    }
}
```

# outmaker
请在 _std函数 里自由发挥，cin输入数据，cout输出数据。
```cpp
#include<bits/stdc++.h>
#define int long long
using namespace std;
void _std()
{
    
}
string ss;
signed main()
{
    for(int i = 1; i <= 20; i ++)
    {
        ss = "";
        int ssum = i;
        while(ssum)
        {
            ss += (ssum % 10) ^ 48;
            ssum /= 10;
        }
        reverse(ss.begin(), ss.end());
        freopen((ss + ".in").c_str(), "r", stdin);
        freopen((ss + ".out").c_str(), "w", stdout);
        _std();
    }
}
```
