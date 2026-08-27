<template>
  <section>
    <div class="page-header">
      <h1>申请单字段配置</h1>
      <div class="page-actions">
        <el-button :icon="Refresh" @click="loadAll">刷新</el-button>
        <el-button :disabled="!isAdmin" @click="saveDraft">保存草稿</el-button>
        <el-button type="primary" :disabled="!isAdmin" @click="publish">发布配置</el-button>
      </div>
    </div>

    <div class="section-band setup-band">
      <el-form label-position="top">
        <el-form-item label="设备配置档">
          <el-select v-model="profileId" @change="loadProfile">
            <el-option v-for="item in profiles" :key="item.id" :label="`${item.name}${item.active ? '（当前）' : ''}`" :value="item.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="申请单类型">
          <el-input v-model="templateName" :disabled="!isAdmin" maxlength="100" />
        </el-form-item>
        <el-form-item label="配置实图">
          <el-select v-model="captureId" filterable @change="loadCapture">
            <el-option v-for="item in captures" :key="item.capture_id" :label="captureLabel(item)" :value="item.capture_id" />
          </el-select>
        </el-form-item>
        <el-form-item label="运行方式">
          <el-switch v-model="automaticEnabled" :disabled="!isAdmin" active-text="固定区域OCR" inactive-text="仅配置" />
        </el-form-item>
      </el-form>
      <div class="setup-actions">
        <el-button
          :icon="Camera"
          :disabled="!isAdmin || captureArmed"
          :loading="armingCapture"
          @click="requestConfigurationCapture"
        >{{ captureArmed ? '等待配置样本' : '采集配置样本' }}</el-button>
        <el-button type="success" :icon="MagicStick" :disabled="!captureId" :loading="testing" @click="generateCurrent">生成患者JSON</el-button>
      </div>
    </div>

    <div class="workspace-grid">
      <div class="section-band image-panel">
        <div class="band-tools annotation-toolbar">
          <h2>实图与OCR文字框</h2>
          <StatusTag v-if="captureDetail" :value="captureDetail.status" />
          <span>{{ blocks.length }} 个文字框</span>
          <el-select v-model="targetFieldKey" class="target-field" placeholder="目标字段">
            <el-option v-for="item in fields" :key="item.field_key" :label="item.display_name" :value="item.field_key" />
          </el-select>
          <el-button :icon="Delete" :disabled="!targetField?.roi_text" @click="clearCurrentRoi">清除区域</el-button>
        </div>
        <div v-if="captureId && !imageError" class="document-stage">
          <div class="document-image-wrap">
            <img :key="imageVersion" :src="captureImageUrl" alt="申请单配置实图" @load="imageLoaded" @error="imageFailed" />
            <div
              ref="overlay"
              class="annotation-overlay"
              @pointerdown="startDraw"
              @pointermove="moveDraw"
              @pointerup="finishDraw"
              @pointercancel="cancelDraw"
            >
              <button
                v-for="block in drawableBlocks"
                :key="`block-${block.id}`"
                type="button"
                class="ocr-box"
                :class="{ low: Number(block.score) < 0.7 }"
                :style="boxStyle(block.normalized_box)"
                :title="`${block.text} · ${confidenceText(block.score)}`"
                @pointerdown.stop
                @click.stop="assignBlock(block)"
              />
              <div
                v-for="field in assignedFields"
                :key="`roi-${field.field_key}`"
                class="field-roi"
                :class="{ selected: field.field_key === targetFieldKey }"
                :style="boxStyle(field.roi)"
                @pointerdown.stop
                @click.stop="targetFieldKey = field.field_key"
              >
                <span>{{ field.display_name }}</span>
              </div>
              <div v-if="draftRoi" class="draft-roi" :style="boxStyle(draftRoi)" />
            </div>
          </div>
        </div>
        <el-empty v-else-if="!captureId" description="暂无摄像头OCR记录" :image-size="72" />
        <el-alert v-else title="该OCR记录没有对应的配置实图" type="warning" :closable="false" show-icon />
      </div>

      <div class="section-band field-panel">
        <div class="band-tools"><h2>字段区域</h2><span>当前：{{ targetField?.display_name || '-' }}</span></div>
        <div class="field-list">
          <button
            v-for="field in fields"
            :key="field.field_key"
            type="button"
            class="field-item"
            :class="{ active: field.field_key === targetFieldKey, assigned: Boolean(field.roi_text), disabled: !field.enabled }"
            @click="targetFieldKey = field.field_key"
          >
            <span><b>{{ field.display_name }}</b><small>{{ field.field_key }}</small></span>
            <el-icon v-if="field.roi_text"><CircleCheck /></el-icon>
          </button>
        </div>
        <el-descriptions v-if="targetField" :column="1" border>
          <el-descriptions-item label="区域">{{ targetField.roi_text || '未配置' }}</el-descriptions-item>
          <el-descriptions-item label="取值">{{ targetField.join_mode === 'reading_order' ? '区域文字拼接' : '区域单值' }}</el-descriptions-item>
          <el-descriptions-item label="校验">{{ fieldValidationText(targetField) }}</el-descriptions-item>
        </el-descriptions>
      </div>
    </div>

    <div class="section-band table-band">
      <div class="band-tools"><h2>字段校验规则</h2><span>坐标基于透视矫正后的标准画布，范围 0～1000</span></div>
      <el-table :data="fields" row-key="field_key" empty-text="暂无字段规则">
        <el-table-column prop="display_name" label="字段" width="125"><template #default="s"><b>{{ s.row.display_name }}</b><small>{{ s.row.field_key }}</small></template></el-table-column>
        <el-table-column label="启用" width="70"><template #default="s"><el-switch v-model="s.row.enabled" :disabled="!isAdmin || !s.row.roi_text" /></template></el-table-column>
        <el-table-column label="必填" width="70"><template #default="s"><el-checkbox v-model="s.row.required" :disabled="!s.row.enabled || !s.row.roi_text || !isAdmin" /></template></el-table-column>
        <el-table-column label="固定区域" min-width="190"><template #default="s"><span>{{ s.row.roi_text || '未配置' }}</span></template></el-table-column>
        <el-table-column label="取值方式" width="145"><template #default="s"><el-select v-model="s.row.join_mode" :disabled="!s.row.enabled || !isAdmin"><el-option label="区域单值" value="single" /><el-option label="文字拼接" value="reading_order" /></el-select></template></el-table-column>
        <el-table-column label="字符" width="115"><template #default="s"><el-select v-model="s.row.char_type" :disabled="!s.row.enabled || !isAdmin"><el-option label="不限" value="any" /><el-option label="纯数字" value="digits" /><el-option label="字母数字" value="alnum" /></el-select></template></el-table-column>
        <el-table-column label="字符数" width="120"><template #default="s"><el-input v-model="s.row.lengths_text" :disabled="!s.row.enabled || !isAdmin" placeholder="如 11,16" /></template></el-table-column>
        <el-table-column label="最低置信度" width="135"><template #default="s"><el-input-number v-model="s.row.min_ocr_score" :disabled="!s.row.enabled || !isAdmin" :min="0" :max="1" :step="0.05" :precision="2" controls-position="right" /></template></el-table-column>
        <el-table-column label="操作" width="90"><template #default="s"><el-button link type="primary" @click="openAdvanced(s.row)">高级</el-button></template></el-table-column>
      </el-table>
    </div>

    <div class="section-band generated-patient">
      <div class="band-tools">
        <h2>结构化患者JSON</h2>
        <StatusTag v-if="generatedStatus" :value="generatedStatus" />
        <span v-else>尚未生成</span>
        <div class="result-actions">
          <el-button :icon="Refresh" :loading="generatedLoading" :disabled="!captureId" @click="loadGenerated">刷新</el-button>
          <el-button :icon="Download" :disabled="!generatedReady" @click="downloadGenerated">下载JSON</el-button>
        </div>
      </div>
      <pre v-if="generatedReady">{{ JSON.stringify(generatedPatient, null, 2) }}</pre>
      <el-empty v-else description="完成字段框选后生成患者JSON" :image-size="64" />
    </div>

    <div v-if="preview" class="section-band patient-preview">
      <div class="band-tools"><h2>字段证据与校验</h2><StatusTag :value="preview.status" /></div>
      <div class="evidence-content">
        <el-descriptions :column="2" border>
          <el-descriptions-item label="缺失字段">{{ preview.missing_fields.join(', ') || '-' }}</el-descriptions-item>
          <el-descriptions-item label="冲突字段">{{ preview.conflict_fields.join(', ') || '-' }}</el-descriptions-item>
          <el-descriptions-item v-for="(item,key) in preview.evidence" :key="key" :label="fieldName(key)">{{ item.value }} · {{ confidenceText(item.score) }} · {{ item.relation }}</el-descriptions-item>
        </el-descriptions>
      </div>
    </div>

    <el-dialog v-model="advancedDialog" :title="`${advancedField?.display_name || ''} · 高级规则`" width="720px">
      <el-form v-if="advancedField" label-width="130px">
        <el-form-item label="标签别名"><el-input v-model="advancedField.alias_text" placeholder="仅用于去除区域中的字段标题" /></el-form-item>
        <el-form-item label="正则表达式"><el-input v-model="advancedField.regex" placeholder="可留空；第一个捕获组作为字段值" /></el-form-item>
        <el-form-item label="最小长度"><el-input-number v-model="advancedField.min_length" :min="0" :max="128" /></el-form-item>
        <el-form-item label="最大长度"><el-input-number v-model="advancedField.max_length" :min="1" :max="512" /></el-form-item>
        <el-form-item label="低分扩框重试"><el-switch v-model="advancedField.expand_once" /></el-form-item>
        <el-form-item label="扩框比例"><el-input-number v-model="advancedField.expand_ratio" :min="0" :max="0.5" :step="0.05" :precision="2" /></el-form-item>
        <el-form-item label="拼接分隔符"><el-input v-model="advancedField.join_separator" maxlength="8" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="advancedDialog=false">完成</el-button></template>
    </el-dialog>
  </section>
</template>

<script setup>
import { computed, defineComponent, h, onBeforeUnmount, onMounted, ref } from "vue";
import { ElMessage } from "element-plus";
import { Camera, CircleCheck, Delete, Download, MagicStick, Refresh } from "@element-plus/icons-vue";
import { api } from "./api";

const props = defineProps({ isAdmin: Boolean });
const StatusTag = defineComponent({props:{value:String},setup(p){const types={accepted:'success',review_required:'warning',rejected:'danger',error:'danger'};return()=>h('span',{class:['status-tag',types[p.value]||'info']},p.value||'-')}});
const definitions = [
  ["patient_name", "患者姓名", ["患者姓名", "姓名"], "any", true], ["patient_id", "患者ID", ["患者ID", "患者编号", "病人ID"], "alnum", true],
  ["his_exam_no", "检查号", ["检查号", "检查单号", "申请号", "申请单号"], "alnum", true], ["report_no", "报告号", ["报告号", "报告单号"], "alnum", true],
  ["exam_item", "检查项目", ["检查项目", "项目名称", "检查名称"], "any", true], ["sex", "性别", ["性别"], "any", true], ["age", "年龄", ["年龄"], "any", true],
  ["birthday", "出生日期", ["出生日期", "出生年月", "出生年月日"], "any", true], ["name_phonetic", "姓名拼音", ["姓名拼音", "拼音"], "any", false],
];
const profiles=ref([]),profileId=ref(null),profileConfig=ref(null),captures=ref([]),captureId=ref(''),captureDetail=ref(null),fields=ref([]),automaticEnabled=ref(false),templateName=ref('申请单1'),preview=ref(null),testing=ref(false),advancedDialog=ref(false),advancedField=ref(null),targetFieldKey=ref('patient_name'),generatedPatient=ref(null),generatedLoading=ref(false),imageError=ref(false),imageVersion=ref(Date.now()),imageSize=ref([]),overlay=ref(null),drawing=ref(null),draftRoi=ref(null),captureArmed=ref(false),armingCapture=ref(false),captureWatchToken=ref(0);
const blocks=computed(()=>captureDetail.value?.payload?.document?.blocks||[]);
const drawableBlocks=computed(()=>blocks.value.filter(item=>validRoi(item.normalized_box)));
const assignedFields=computed(()=>fields.value.filter(item=>validRoi(item.roi)));
const targetField=computed(()=>fields.value.find(item=>item.field_key===targetFieldKey.value)||null);
const captureImageUrl=computed(()=>captureId.value?`/api/v1/camera-captures/${encodeURIComponent(captureId.value)}/image?v=${imageVersion.value}`:'');
const generatedReady=computed(()=>Boolean(generatedPatient.value&&generatedPatient.value.code!=='NOT_READY'));
const generatedStatus=computed(()=>{if(!generatedReady.value)return'';if(generatedPatient.value.success)return'accepted';return{REVIEW_REQUIRED:'review_required',FAIL:'rejected',ERROR:'error'}[generatedPatient.value.code]||'review_required'});

function makeField([field_key,display_name,aliases,char_type]){return{field_key,target:field_key,display_name,enabled:false,required:false,alias_text:aliases.join(', '),label_aliases:[...aliases],char_type,lengths_text:'',lengths:[],min_length:0,max_length:10000,min_ocr_score:0.65,match_mode:'label_assisted',join_mode:'single',join_separator:'',expand_once:true,expand_ratio:0.10,regex:'',roi:null,roi_text:''}}
function mergeFields(existing=[]){const map=new Map(existing.filter(x=>x&&x.field_key).map(x=>[x.field_key,x]));return definitions.map(def=>{const base=makeField(def),saved=map.get(base.field_key)||{},row={...base,...saved,display_name:base.display_name};row.alias_text=(Array.isArray(saved.label_aliases)?saved.label_aliases:base.label_aliases).join(', ');row.lengths_text=(Array.isArray(saved.lengths)?saved.lengths:[]).join(',');row.roi=validRoi(saved.roi)?saved.roi.map(Number):null;row.roi_text=row.roi?row.roi.map(x=>Math.round(x)).join(','):'';row.enabled=Boolean(row.roi&&saved.enabled!==false);row.match_mode=row.roi?'fixed_roi':'label_assisted';return row})}
function serializeFields(){return fields.value.map(row=>{const aliases=row.alias_text.split(/[,，]/).map(x=>x.trim()).filter(Boolean),lengths=row.lengths_text.split(/[,，\s]+/).map(Number).filter(x=>Number.isInteger(x)&&x>0),roi=parseRoi(row.roi_text);return{field_key:row.field_key,target:row.field_key,enabled:Boolean(row.enabled&&roi),required:Boolean(row.required&&roi),label_aliases:[...new Set(aliases)],char_type:row.char_type,lengths:[...new Set(lengths)],min_length:Number(row.min_length||0),max_length:Number(row.max_length||10000),min_ocr_score:Number(row.min_ocr_score||0),match_mode:roi?'fixed_roi':'label_assisted',roi,join_mode:row.join_mode||'single',join_separator:row.join_separator||'',expand_once:Boolean(row.expand_once),expand_ratio:Number(row.expand_ratio||0),relations:['same_text','same_line_right','next_line_same_column'],regex:row.regex||''}})}
function currentResolver(){return{provider:'rules',fields:serializeFields()}}
function cloneConfig(value){return JSON.parse(JSON.stringify(value))}
function captureLabel(item){return `${new Date(item.received_at*1000).toLocaleString()} · ${item.status} · ${item.capture_id.slice(0,8)}`}
function confidenceText(value){return Number.isFinite(Number(value))?`${(Number(value)*100).toFixed(1)}%`:'-'}
function fieldName(key){return definitions.find(x=>x[0]===key)?.[1]||key}
function validRoi(value){if(!Array.isArray(value)||value.length!==4)return false;const [l,t,r,b]=value.map(Number);return [l,t,r,b].every(Number.isFinite)&&l>=0&&t>=0&&r<=1000&&b<=1000&&l<r&&t<b}
function parseRoi(text){const values=String(text||'').split(/[,，\s]+/).map(Number);return validRoi(values)?values.map(value=>Math.round(value*1000)/1000):null}
function boxStyle(value){if(!validRoi(value))return{};const [l,t,r,b]=value.map(Number);return{left:`${l/10}%`,top:`${t/10}%`,width:`${(r-l)/10}%`,height:`${(b-t)/10}%`}}
function fieldValidationText(field){const parts=[field.char_type==='digits'?'纯数字':field.char_type==='alnum'?'字母数字':'不限字符'];if(field.lengths_text)parts.push(`${field.lengths_text}位`);parts.push(`≥${confidenceText(field.min_ocr_score)}`);return parts.join(' · ')}

async function loadAll(){const [p,c,s]=await Promise.all([api('/api/v1/profiles'),api('/api/v1/camera-captures?limit=30'),api('/api/v1/camera/configuration-capture')]);profiles.value=p.items;captures.value=c.items;captureArmed.value=Boolean(s.armed);if(!profileId.value)profileId.value=(profiles.value.find(x=>x.active)||profiles.value[0])?.id||null;if(!captureId.value)captureId.value=captures.value[0]?.capture_id||'';await loadProfile();await loadCapture()}
async function loadProfile(){if(!profileId.value)return;const detail=await api('/api/v1/profiles/'+profileId.value),revision=detail.revisions.find(x=>x.status==='draft')||detail.revisions.find(x=>x.id===detail.current_revision_id)||detail.revisions[0];profileConfig.value=cloneConfig(revision.config);automaticEnabled.value=Boolean(profileConfig.value.camera_patient_enabled);templateName.value=profileConfig.value.camera_template?.name||'申请单1';fields.value=mergeFields(profileConfig.value.field_resolver?.fields||[]);const reference=profileConfig.value.camera_template?.reference_capture_id;if(reference&&captures.value.some(item=>item.capture_id===reference))captureId.value=reference;preview.value=null}
async function loadCapture(){if(!captureId.value){captureDetail.value=null;generatedPatient.value=null;return}imageError.value=false;imageSize.value=[];imageVersion.value=Date.now();const [detail]=await Promise.all([api('/api/v1/camera-captures/'+captureId.value),loadGenerated()]);captureDetail.value=detail;preview.value=null}
async function loadGenerated(){if(!captureId.value){generatedPatient.value=null;return}generatedLoading.value=true;try{generatedPatient.value=await api(`/api/v1/camera-captures/${captureId.value}/patient`)}finally{generatedLoading.value=false}}
function imageLoaded(event){imageError.value=false;imageSize.value=[event.target.naturalWidth,event.target.naturalHeight]}
function imageFailed(){imageError.value=true;imageSize.value=[]}
function assignBlock(block){if(!targetField.value)return ElMessage.warning('请先选择目标字段');const [l,t,r,b]=block.normalized_box.map(Number),padX=Math.max(5,(r-l)*0.12),padY=Math.max(5,(b-t)*0.35);setFieldRoi([Math.max(0,l-padX),Math.max(0,t-padY),Math.min(1000,r+padX),Math.min(1000,b+padY)]);ElMessage.success(`已绑定到${targetField.value.display_name}`)}
function setFieldRoi(roi){if(!targetField.value||!validRoi(roi))return;targetField.value.roi=roi.map(value=>Math.round(value));targetField.value.roi_text=targetField.value.roi.join(',');targetField.value.match_mode='fixed_roi';targetField.value.enabled=true}
function pointFromEvent(event){const rect=overlay.value.getBoundingClientRect();return{x:Math.max(0,Math.min(1000,(event.clientX-rect.left)*1000/rect.width)),y:Math.max(0,Math.min(1000,(event.clientY-rect.top)*1000/rect.height))}}
function startDraw(event){if(!props.isAdmin||!targetField.value)return;event.currentTarget.setPointerCapture(event.pointerId);const point=pointFromEvent(event);drawing.value={id:event.pointerId,start:point};draftRoi.value=[point.x,point.y,point.x+1,point.y+1]}
function moveDraw(event){if(!drawing.value||drawing.value.id!==event.pointerId)return;const point=pointFromEvent(event),start=drawing.value.start;draftRoi.value=[Math.min(start.x,point.x),Math.min(start.y,point.y),Math.max(start.x,point.x),Math.max(start.y,point.y)]}
function finishDraw(event){if(!drawing.value||drawing.value.id!==event.pointerId)return;moveDraw(event);const roi=draftRoi.value;drawing.value=null;draftRoi.value=null;if(roi&&(roi[2]-roi[0])>=5&&(roi[3]-roi[1])>=5)setFieldRoi(roi)}
function cancelDraw(){drawing.value=null;draftRoi.value=null}
function clearCurrentRoi(){if(!targetField.value)return;targetField.value.roi=null;targetField.value.roi_text='';targetField.value.match_mode='label_assisted';targetField.value.enabled=false;targetField.value.required=false}

function downloadGenerated(){if(!generatedReady.value)return;const blob=new Blob([JSON.stringify(generatedPatient.value,null,2)+'\n'],{type:'application/json;charset=utf-8'}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=`patient-${captureId.value.slice(0,12)}.json`;document.body.appendChild(link);link.click();link.remove();URL.revokeObjectURL(url)}
async function generateCurrent(){if(!captureId.value)return;testing.value=true;try{preview.value=await api(`/api/v1/camera-captures/${captureId.value}/resolve-patient`,{method:'POST',body:JSON.stringify({field_resolver:currentResolver(),persist:true})});generatedPatient.value=preview.value.response;ElMessage.success('患者JSON已生成并保存')}catch(error){ElMessage.error(error.message)}finally{testing.value=false}}
async function requestConfigurationCapture(){if(!props.isAdmin)return;const baseline=captures.value[0]?.capture_id||'';armingCapture.value=true;try{const result=await api('/api/v1/camera/configuration-capture',{method:'POST',body:'{}'});captureArmed.value=Boolean(result.armed);ElMessage.success('已等待整页配置样本');watchConfigurationCapture(baseline,++captureWatchToken.value)}catch(error){ElMessage.error(error.message)}finally{armingCapture.value=false}}
async function watchConfigurationCapture(baseline,token){for(let attempt=0;attempt<45;attempt+=1){await new Promise(resolve=>setTimeout(resolve,2000));if(token!==captureWatchToken.value)return;try{const [status,list]=await Promise.all([api('/api/v1/camera/configuration-capture'),api('/api/v1/camera-captures?limit=30')]);captureArmed.value=Boolean(status.armed);captures.value=list.items;const candidate=captures.value.find(item=>item.capture_id!==baseline);if(candidate){const detail=await api('/api/v1/camera-captures/'+candidate.capture_id);if(detail.payload?.source?.recognition_mode==='configuration_full_page'){captureId.value=candidate.capture_id;await loadCapture();ElMessage.success('配置实图和OCR文字框已更新');return}}}catch{/* 下一轮继续等待 */}}ElMessage.warning('配置样本等待超时，请确认报告单已取走后重新放入')}
async function persistDraft(announce=true){const config=cloneConfig(profileConfig.value);config.camera_patient_enabled=automaticEnabled.value;config.field_resolver=currentResolver();config.camera_template={schema_version:1,id:config.camera_template?.id||'default',name:templateName.value.trim()||'申请单1',mode:'fixed_roi',selection_mode:'manual',reference_capture_id:imageSize.value.length===2?captureId.value:(config.camera_template?.reference_capture_id||''),canonical_image_size:imageSize.value.length===2?[...imageSize.value]:(config.camera_template?.canonical_image_size||[])};await api(`/api/v1/profiles/${profileId.value}/draft`,{method:'PUT',body:JSON.stringify({config})});profileConfig.value=config;if(announce)ElMessage.success('固定区域配置草稿已保存');return config}
async function saveDraft(){if(!props.isAdmin)return;try{await persistDraft()}catch(error){ElMessage.error(error.message)}}
async function publish(){if(!props.isAdmin)return;try{await persistDraft(false);await api(`/api/v1/profiles/${profileId.value}/publish`,{method:'POST',body:'{}'});await loadAll();if(captureId.value)await generateCurrent();ElMessage.success('固定区域配置已发布')}catch(error){ElMessage.error(error.message)}}
function openAdvanced(row){advancedField.value=row;advancedDialog.value=true}
onMounted(()=>loadAll().catch(error=>ElMessage.error(error.message)));
onBeforeUnmount(()=>{captureWatchToken.value+=1});
</script>

<style scoped>
.setup-band{display:flex;align-items:end}.setup-band .el-form{display:grid;grid-template-columns:minmax(180px,.8fr) minmax(180px,.8fr) minmax(280px,1.3fr) minmax(170px,.7fr);gap:12px;flex:1}.setup-band .el-form-item{margin:0}.setup-actions{padding:0 0 2px 14px}.workspace-grid{display:grid;grid-template-columns:minmax(520px,1.7fr) minmax(250px,.65fr);gap:14px}.image-panel,.field-panel,.generated-patient,.patient-preview{padding:0}.annotation-toolbar{flex-wrap:wrap}.target-field{margin-left:auto;width:180px}.document-stage{display:grid;place-items:center;min-height:520px;max-height:72vh;padding:14px;overflow:auto;background:#202729}.document-image-wrap{position:relative;display:inline-block;max-width:100%;line-height:0;box-shadow:0 3px 18px rgba(0,0,0,.28)}.document-image-wrap img{display:block;max-width:100%;max-height:calc(72vh - 28px);width:auto;height:auto;user-select:none}.annotation-overlay{position:absolute;inset:0;cursor:crosshair;touch-action:none}.ocr-box{position:absolute;padding:0;border:1px solid rgba(255,189,46,.9);background:rgba(255,189,46,.08);border-radius:1px;cursor:pointer}.ocr-box:hover{border-width:2px;background:rgba(255,189,46,.22)}.ocr-box.low{border-color:#e35c5c;background:rgba(227,92,92,.1)}.field-roi,.draft-roi{position:absolute;border:2px solid #08a184;background:rgba(8,161,132,.10);pointer-events:auto;cursor:pointer}.field-roi.selected{border-color:#087e8b;background:rgba(8,126,139,.18);box-shadow:0 0 0 1px #fff}.field-roi span{position:absolute;left:-2px;top:-22px;line-height:20px;padding:0 5px;background:#087e8b;color:#fff;font-size:12px;white-space:nowrap}.draft-roi{border-style:dashed;pointer-events:none}.field-panel .band-tools{flex-wrap:wrap}.field-list{display:grid;gap:1px;background:#dce4e6;border-bottom:1px solid #dce4e6}.field-item{height:54px;border:0;background:#fff;padding:8px 12px;display:flex;align-items:center;justify-content:space-between;text-align:left;color:#25343a;cursor:pointer}.field-item:hover{background:#f3f8f8}.field-item.active{box-shadow:inset 3px 0 #087e8b;background:#eaf5f5}.field-item.assigned .el-icon{color:#16825e}.field-item.disabled{opacity:.48}.field-item small{display:block;color:#738187;margin-top:2px}.field-panel .el-descriptions{margin:14px}.generated-patient>pre,.evidence-content{margin:14px}.result-actions{display:flex;gap:8px;margin-left:auto}.result-actions .el-button{margin-left:0}
@media(max-width:1100px){.workspace-grid{grid-template-columns:1fr}.field-list{grid-template-columns:repeat(3,1fr)}.setup-band{align-items:stretch;flex-direction:column}.setup-band .el-form{grid-template-columns:repeat(2,minmax(180px,1fr));width:100%}.setup-actions{padding:12px 0 0}}
@media(max-width:900px){.page-header{display:block;height:auto}.page-actions{margin:10px 0 0;flex-wrap:wrap}.setup-band .el-form{grid-template-columns:1fr}.document-stage{min-height:320px;max-height:60vh}.document-image-wrap img{max-height:calc(60vh - 28px)}.field-list{grid-template-columns:repeat(2,1fr)}.annotation-toolbar{align-items:flex-start}.target-field{margin-left:0;width:100%}.generated-patient .band-tools{align-items:flex-start;flex-wrap:wrap}.result-actions{margin-left:0;width:100%}}
</style>
