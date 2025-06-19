# 真实道路样例与标注流程

[返回首页](../README.md) · [样例标注 JSON](real-examples.json) · [图片署名](images/real/ATTRIBUTION.md)

裂缝样例来自 Cracks and Potholes in Road Images，展示开口明显的纵向裂缝。其余样例来自 RoadSens-4M 视频，分数为项目 v4 初版观测记录，各类独立取值 `0–1`，供人工复核。

## 样例说明

| 样例 | 来源定位 | 标注信息 | 复核重点 |
| --- | --- | --- | --- |
| [裂缝](images/real/cracks-open.jpg) | 原图 `918387_RS_290_290RS294095_25385.jpg` | 数据集附裂缝掩膜 | 检查纵向开口、破裂边缘与细小分叉 |
| [铺装破损](images/real/breakage.jpg) | S060 · 帧 215 · 7.165 秒 | 剥落／破损 0.99，散料 0.90 | 检查材料缺失与破碎边缘，分别判断伴随异常 |
| [积水](images/real/water.jpg) | S034 · 帧 91 · 3.033 秒 | 积水 0.95 | 确认水面，坑洞判断另查可见证据 |
| [正常对照](images/real/normal.jpg) | S059 · 帧 163 · 5.432 秒 | 正常 0.90 | 标线和阴影不计异常 |

## 标注与复核

![真实样例复核工作台](images/real/workbench.png)

1. **取得初标**：选择模型、抽帧密度与分类 Skills，生成逐帧分数和依据；已有记录可导入复核。
2. **定位画面**：通过帧号或时间轴查看原始帧，结合邻帧检查异常是否可见。
3. **修正判断**：逐类修改置信度、填写备注，确认或保存待复核；保留原始判断与修改历史。
4. **导出候选**：选择类别、阈值与连接间隔，生成供后续图像标注使用的时序区间。

截图展示 S060 的 v4 导入记录与路面裁切，状态为待复核。

## 候选筛选

默认选择裂缝、坑洞／凹陷、剥落／破损、异常修补，阈值 **0.80**，相邻合并间隔 **0.20 秒**；积水、散料和异物可独立加入。任一所选类别达到阈值即命中，区间合并不改动逐帧标签。

例如 `[7.10, 7.20)` 与 `[7.35, 7.50)` 相隔 `0.15 秒`，按上述规则合并为 `[7.10, 7.50)`。这是规则示例，未引用样例视频的实际边界。

未分析或看不清的帧可保留为未知；排除初标不会自动标为正常。候选时长占比用于衡量筛选范围，不等同于召回率。

## 来源与数据

[样例 JSON](real-examples.json) 记录来源和裁切信息；视频样例另含九类分数、原始备注、帧号与时间。裂缝为静态图像示例，不附推理置信度。

裂缝：Bianka T. Passos 等，[Cracks and Potholes in Road Images v4](https://data.mendeley.com/datasets/t576ydh9v8/4)；其余图片：Amith Khandakar 等，[RoadSens-4M Dataset v3](https://figshare.com/articles/dataset/RoadSens-4M_Dataset/30341143/3)，[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)。完整署名与处理方式见 [ATTRIBUTION.md](images/real/ATTRIBUTION.md)。
