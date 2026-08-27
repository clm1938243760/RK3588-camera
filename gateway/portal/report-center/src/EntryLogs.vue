<template>
  <section>
    <div class="page-header">
      <h1>录入日志</h1>
      <div class="page-actions">
        <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
      </div>
    </div>
    <div class="filter-row entry-log-filters">
      <el-select v-model="statusFilter" clearable placeholder="全部录入状态" @change="load">
        <el-option label="进行中" value="running" />
        <el-option label="已完成" value="completed" />
        <el-option label="失败" value="failed" />
      </el-select>
      <el-button type="primary" :icon="Search" @click="load">查询</el-button>
    </div>
    <div class="section-band table-band">
      <el-table :data="logs.items" v-loading="loading" empty-text="暂无录入记录">
        <el-table-column label="录入时间" width="175">
          <template #default="scope">{{ timeText(scope.row.started_at || scope.row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="患者" min-width="145">
          <template #default="scope">
            <b>{{ scope.row.patient?.patient_name || "未识别" }}</b>
            <small>{{ scope.row.patient?.patient_id || "" }}</small>
          </template>
        </el-table-column>
        <el-table-column label="录入字段" min-width="280">
          <template #default="scope">
            <div class="entry-field-summary">
              <span v-for="(value, key) in visibleFields(scope.row.fields)" :key="key">
                <strong>{{ fieldLabel(key) }}</strong>：{{ value }}
              </span>
              <span v-if="!Object.keys(visibleFields(scope.row.fields)).length" class="entry-muted">无字段值</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="scope"><StatusTag :value="scope.row.status" /></template>
        </el-table-column>
        <el-table-column label="原图" width="110">
          <template #default="scope">
            <el-button v-if="scope.row.image_available" link type="primary" @click="viewImage(scope.row)">查看</el-button>
            <span v-else class="entry-muted">不可用</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="190">
          <template #default="scope">
            <el-button link type="primary" @click="openDetail(scope.row.id)">详情</el-button>
            <el-button v-if="scope.row.image_available" link type="primary" @click="downloadImage(scope.row)">下载原图</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-pagination
        v-model:current-page="logs.page"
        v-model:page-size="logs.page_size"
        :total="logs.total"
        layout="total, prev, pager, next"
        @current-change="load"
      />
    </div>

    <el-dialog v-model="detailVisible" title="录入详情" width="760px">
      <el-descriptions v-if="detail" :column="2" border>
        <el-descriptions-item label="录入时间">{{ timeText(detail.started_at) }}</el-descriptions-item>
        <el-descriptions-item label="完成时间">{{ timeText(detail.finished_at) }}</el-descriptions-item>
        <el-descriptions-item label="患者姓名">{{ detail.patient?.patient_name || "-" }}</el-descriptions-item>
        <el-descriptions-item label="患者ID">{{ detail.patient?.patient_id || "-" }}</el-descriptions-item>
        <el-descriptions-item label="录入状态"><StatusTag :value="detail.status" /></el-descriptions-item>
        <el-descriptions-item label="动作数">{{ detail.action_count }}</el-descriptions-item>
        <el-descriptions-item label="会话ID" :span="2"><span class="hash-text">{{ detail.session_id }}</span></el-descriptions-item>
        <el-descriptions-item v-if="detail.error" label="失败原因" :span="2"><span class="bad">{{ detail.error }}</span></el-descriptions-item>
        <el-descriptions-item v-if="detail.image_error" label="原图说明" :span="2"><span class="entry-muted">{{ detail.image_error }}</span></el-descriptions-item>
      </el-descriptions>
      <h3>本次录入字段</h3>
      <pre v-if="detail">{{ JSON.stringify(detail.fields || {}, null, 2) }}</pre>
      <template #footer>
        <el-button v-if="detail?.image_available" @click="viewImage(detail)">查看高清原图</el-button>
        <el-button v-if="detail?.image_available" type="primary" @click="downloadImage(detail)">下载高清原图</el-button>
        <el-button @click="detailVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="imageVisible" title="录入时高清原图" width="900px" top="5vh">
      <div class="entry-image-viewer"><img v-if="imageUrl" :src="imageUrl" alt="录入时高清原图" /></div>
      <template #footer><el-button type="primary" @click="downloadImage(imageLog)">下载原图</el-button><el-button @click="imageVisible = false">关闭</el-button></template>
    </el-dialog>
  </section>
</template>

<script setup>
import { defineComponent, h, reactive, ref, onMounted } from "vue";
import { ElMessage } from "element-plus";
import { Refresh, Search } from "@element-plus/icons-vue";
import { api } from "./api";

const StatusTag = defineComponent({
  props: { value: String },
  setup(props) {
    const labels = { running: "进行中", completed: "已完成", failed: "失败" };
    return () => h(
      "span",
      { class: ["status-tag", props.value === "completed" ? "success" : props.value === "failed" ? "danger" : "warning"] },
      labels[props.value] || props.value || "-",
    );
  },
});
const logs = reactive({ items: [], page: 1, page_size: 20, total: 0 });
const statusFilter = ref("");
const loading = ref(false);
const detailVisible = ref(false), detail = ref(null);
const imageVisible = ref(false), imageUrl = ref(""), imageLog = ref(null);
const labels = { patient_name: "姓名", patient_id: "患者ID", exam_item: "检查项目", his_exam_no: "检查号", report_no: "报告号", sex: "性别", age: "年龄", birthday: "出生日期", name_phonetic: "拼音" };

function timeText(value) { return value ? new Date(Number(value) * 1000).toLocaleString() : "-"; }
function fieldLabel(key) { return labels[key] || key; }
function visibleFields(fields) {
  if (!fields || typeof fields !== "object") return {};
  return Object.fromEntries(Object.entries(fields).filter(([key, value]) => key !== "extra_fields" && value !== null && value !== ""));
}
async function load() {
  loading.value = true;
  try {
    const query = new URLSearchParams({ page: String(logs.page), page_size: String(logs.page_size) });
    if (statusFilter.value) query.set("status", statusFilter.value);
    Object.assign(logs, await api("/api/v1/entry-logs?" + query));
  } catch (error) {
    ElMessage.error(error.message);
  } finally { loading.value = false; }
}
async function openDetail(id) {
  try { detail.value = await api("/api/v1/entry-logs/" + encodeURIComponent(id)); detailVisible.value = true; }
  catch (error) { ElMessage.error(error.message); }
}
function viewImage(log) { imageLog.value = log; imageUrl.value = "/api/v1/entry-logs/" + encodeURIComponent(log.id) + "/image?v=" + Date.now(); imageVisible.value = true; }
function downloadImage(log) { window.open("/api/v1/entry-logs/" + encodeURIComponent(log.id) + "/image?download=1", "_blank"); }
onMounted(load);
</script>

<style scoped>
.entry-field-summary { display: flex; flex-wrap: wrap; gap: 4px 12px; line-height: 1.7; }
.entry-field-summary span { white-space: nowrap; }
.entry-field-summary strong { color: #536a70; font-weight: 600; }
.entry-muted { color: #738187; }
.entry-image-viewer { max-height: 72vh; overflow: auto; background: #202729; padding: 12px; text-align: center; }
.entry-image-viewer img { max-width: 100%; height: auto; display: inline-block; }
</style>
