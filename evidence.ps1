Get-Content data\run_latest.log -Encoding UTF8 | Select-String -Pattern '票价系数|过滤前|过滤后|方案对比|排除诊断|追踪|源价对比|观测落库|API统计|采集分析推送结束|Traceback|合并选价' | Select-Object -ExpandProperty Line
