# Ubuntu-Configure-Tools

用于ANTL实验室服务器与交换机的各类基础配置，针对实物平台开发的各类自动化脚本。

## 内容目录

| # | 模块 | 简介 | 目录 |
| :-: | :-- | :-- | :-- |
| 1 | [TOR自动配置脚本（tor-ovs）](#tor-ovs) | OVS 网桥初始化与 OpenFlow 流配置 | [tor-ovs/](tor-ovs/) |
| 2 | [光交换机socket控制脚本（ocs-sokect-control）](#ocs-sokect-control) | Polatis 光交换机串口控制 | [ocs-sokect-control/](ocs-sokect-control/) |
| 3 | [APP流量生成脚本（app）](#app) | 多机架流量生成 | [app/](app/) |
| 4 | [推理相关脚本与工具（inference）](#inference) | 部署配置与性能监控 | [inference/](inference/) |
| 5 | [CX系列网卡相关控制脚本（nic-control）](#nic-control) | 命名空间隔离与静态 ARP配置 | [nic-control/](nic-control/) |
| 6 | [Agent 服务管理脚本（agent）](#agent) | Dify 服务的启动、停止与健康检查 | [agent/](agent/) |
