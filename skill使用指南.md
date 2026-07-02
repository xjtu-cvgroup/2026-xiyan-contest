skill地址：https://agent.huawei.com/ai/skills/lychee-demo-movement-starter?version=0.0.1&tab=detail

安装方式：


**对话安装:**

复制下方安装指引到你的 Agent 对话中，即可快速安装当前Skill
```
1. 根据 https://agent.huawei.com/mcpService/v1/install/agentcenter/download  安装 agentcenter cli 和 agentcenter-skill-finder
2. 使用agentcenter-skill-finder，快速安装"lychee-demo-movement-starter"
```

**市场安装**
第一步:
npm config set @aimarket:registry=https://cmc.centralrepo.rnd.huawei.com/artifactory/api/npm/product_npm/ strict-ssl=false
第二步:
npx @aimarket/agentcenter skill add lychee-demo-movement-starter@0.0.1
可选参数 -g：添加该参数可全局安装，不传则安装到当前项目

**安装命令**

**CodeAgent TUI**
```
/skill add lychee-demo-movement-starter@0.0.1
```
**CodeAgent CLI**
```
nga skill add lychee-demo-movement-starter@0.0.1
```
**CodeAgent CLI（1.2601.1-1及之前版本）**
```
/skill add lychee-demo-movement-starter@0.0.1
```
**CAC**
```
codeagent skills add lychee-demo-movement-starter@0.0.1
```

安装完成后使用：

在对应基础程序路径下打开codeAgent:
```
/lychee-demo-movement-starter
```
**注意**：
调测指南.md 的路径必须给本地调测包中的绝对路径