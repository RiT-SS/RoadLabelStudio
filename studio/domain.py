"""Annotation semantics shared by API, workers and export."""
import math
from pydantic import BaseModel, Field, model_validator

CLASSES = {
    'crack': '裂缝', 'pothole_depression': '坑洞／凹陷',
    'surface_breakage': '剥落／破损', 'irregular_repair': '异常修补',
    'water': '积水', 'mud_loose_material': '积泥／散料',
    'foreign_object': '异物', 'normal': '正常', 'unclear_visibility': '看不清',
}
KEYS = list(CLASSES)
RULES = [
    '沿道路表面检查细长、不连续或网状裂纹。区分完整施工接缝、标线与阴影；有疑点时降低置信度，不因正常路段背景排除裂缝。',
    '寻找材料缺失和低于周围路面的凹陷，结合坑缘、内部纹理与阴影判断。单帧不能仅凭阴影或车辆颠簸确认坑洞。',
    '检查表面剥落、碎裂、缺损和松散边缘。均匀骨料纹理不直接作为破损，清楚指出异常区域的可见依据。',
    '只将凸凹、碎裂、缺料或不完整的修补计为异常；完整平坦修补及颜色变化不自动视为病害。',
    '检查可通行路面上的积水及其反光或水面边界。与阴影、湿润但无积水路面区分；不能据积水推断下方一定存在坑洞。',
    '寻找侵入通行路面的泥块、泥堆、碎石和散料。土路材质、均匀薄尘不自动计为异常。',
    '寻找通行路面内的枝叶、垃圾、砖块及其他障碍物。明确位于路外的物体、正常行人车辆不计入此类。',
]

class Scores(BaseModel):
    scores: dict[str, float]
    note: str = Field(default='', max_length=4000)

    @model_validator(mode='after')
    def check(self):
        if set(self.scores) != set(KEYS):
            raise ValueError('每帧必须包含全部 9 类的独立置信度')
        if any(not math.isfinite(x) or not 0 <= x <= 1 for x in self.scores.values()):
            raise ValueError('置信度必须是 0～1 的有限数值')
        if self.scores['normal'] >= .5 and max(self.scores[k] for k in KEYS if k != 'normal') >= .5:
            raise ValueError('正常不能同时与较确定的异常或看不清并存')
        return self

class ReviewEdit(Scores):
    status: str = Field(pattern='^(pending|confirmed|rejected)$')
    expected_revision: int = Field(ge=0)

class SkillEdit(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    instructions: str = Field(min_length=10, max_length=6000)
    enabled: bool = True
    expected_revision: int = Field(ge=0)

class JobRequest(BaseModel):
    provider: str = Field(pattern='^(qwen|deepseek)$')
    sampling: str = Field(default='sampled', pattern='^(sampled|every_frame)$')
    fps: float = Field(default=1, ge=.2, le=10)
    skill_ids: list[str] = Field(min_length=1, max_length=7)

def candidates(pts, duration, rows, selected, threshold=.8, gap=.2, keep_unknown=True):
    """No interpolation of sampled frames; [start,end), source labels untouched.

    A rejected proposal becomes unassessed, not a fabricated normal label.
    """
    lookup = {r['frame_index']: r for r in rows}
    spans = []
    for i, start in enumerate(pts):
        end = pts[i+1] if i+1 < len(pts) else duration
        row = lookup.get(i)
        unknown = row is None or row['status'] == 'rejected'
        if row and not unknown:
            scores = row['scores']
            unknown = scores['unclear_visibility'] >= .5 or max(scores.values()) == 0
        positive = row is not None and row['status'] != 'rejected' and any(row['scores'][k] >= threshold for k in selected)
        if positive or (keep_unknown and unknown):
            if spans and start - spans[-1]['end_s'] <= gap + 1e-9:
                spans[-1]['end_s'] = end
                spans[-1]['contains_unassessed'] |= unknown
            else:
                spans.append({'start_s': start, 'end_s': end, 'contains_unassessed': unknown})
    return spans
