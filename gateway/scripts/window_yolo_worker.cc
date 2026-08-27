#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include <opencv2/opencv.hpp>
#include <unistd.h>

#include "rknn_api.h"

struct Detection {
    float x1;
    float y1;
    float x2;
    float y2;
    float score;
};

struct LetterBox {
    float scale;
    int pad_x;
    int pad_y;
    int src_w;
    int src_h;
};

class WindowYoloWorker {
public:
    ~WindowYoloWorker() {
        if (outputs_) {
            std::free(outputs_);
        }
        if (ctx_) {
            rknn_destroy(ctx_);
        }
    }

    bool init(const std::string& model_path, std::string* error) {
        std::vector<unsigned char> model_data;
        if (!read_file(model_path, &model_data, error)) {
            return false;
        }
        int ret = rknn_init(&ctx_, model_data.data(), static_cast<uint32_t>(model_data.size()), 0, NULL);
        if (ret != RKNN_SUCC) {
            *error = "rknn_init failed: " + std::to_string(ret);
            return false;
        }

        std::memset(&io_num_, 0, sizeof(io_num_));
        ret = rknn_query(ctx_, RKNN_QUERY_IN_OUT_NUM, &io_num_, sizeof(io_num_));
        if (ret != RKNN_SUCC || io_num_.n_input < 1 || io_num_.n_output != 1) {
            *error = "unexpected io num";
            return false;
        }

        std::memset(&input_attr_, 0, sizeof(input_attr_));
        input_attr_.index = 0;
        ret = rknn_query(ctx_, RKNN_QUERY_INPUT_ATTR, &input_attr_, sizeof(input_attr_));
        if (ret != RKNN_SUCC) {
            *error = "query input attr failed: " + std::to_string(ret);
            return false;
        }

        std::memset(&output_attr_, 0, sizeof(output_attr_));
        output_attr_.index = 0;
        ret = rknn_query(ctx_, RKNN_QUERY_OUTPUT_ATTR, &output_attr_, sizeof(output_attr_));
        if (ret != RKNN_SUCC) {
            *error = "query output attr failed: " + std::to_string(ret);
            return false;
        }

        if (input_attr_.n_dims == 4) {
            if (input_attr_.fmt == RKNN_TENSOR_NHWC) {
                input_h_ = static_cast<int>(input_attr_.dims[1]);
                input_w_ = static_cast<int>(input_attr_.dims[2]);
            } else {
                input_h_ = static_cast<int>(input_attr_.dims[2]);
                input_w_ = static_cast<int>(input_attr_.dims[3]);
            }
        }

        outputs_ = static_cast<rknn_output*>(std::calloc(io_num_.n_output, sizeof(rknn_output)));
        if (!outputs_) {
            *error = "allocate output failed";
            return false;
        }
        outputs_[0].index = 0;
        outputs_[0].want_float = 1;
        outputs_[0].is_prealloc = 0;
        return true;
    }

    bool run(const std::string& image_path, float conf_thresh, float iou_thresh, int max_det,
             std::vector<Detection>* detections, long* elapsed_ms, std::string* error) {
        auto started = std::chrono::steady_clock::now();
        cv::Mat bgr = cv::imread(image_path, cv::IMREAD_COLOR);
        if (bgr.empty()) {
            *error = "failed to read image: " + image_path;
            return false;
        }

        LetterBox meta;
        cv::Mat rgb = letterbox_rgb(bgr, input_w_, input_h_, &meta);

        rknn_input input;
        std::memset(&input, 0, sizeof(input));
        input.index = 0;
        input.buf = rgb.data;
        input.size = static_cast<uint32_t>(input_w_ * input_h_ * 3);
        input.pass_through = 0;
        input.type = RKNN_TENSOR_UINT8;
        input.fmt = RKNN_TENSOR_NHWC;

        int ret = rknn_inputs_set(ctx_, 1, &input);
        if (ret != RKNN_SUCC) {
            *error = "rknn_inputs_set failed: " + std::to_string(ret);
            return false;
        }

        ret = rknn_run(ctx_, NULL);
        if (ret != RKNN_SUCC) {
            *error = "rknn_run failed: " + std::to_string(ret);
            return false;
        }

        ret = rknn_outputs_get(ctx_, io_num_.n_output, outputs_, NULL);
        if (ret != RKNN_SUCC) {
            *error = "rknn_outputs_get failed: " + std::to_string(ret);
            return false;
        }

        const float* data = static_cast<const float*>(outputs_[0].buf);
        int elem_count = static_cast<int>(output_attr_.n_elems);
        *detections = parse_yolov8_output(data, elem_count, output_attr_, input_w_, input_h_, meta,
                                          conf_thresh, iou_thresh, max_det);
        rknn_outputs_release(ctx_, io_num_.n_output, outputs_);
        auto finished = std::chrono::steady_clock::now();
        *elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(finished - started).count();
        return true;
    }

private:
    static bool read_file(const std::string& path, std::vector<unsigned char>* data, std::string* error) {
        std::ifstream file(path.c_str(), std::ios::binary | std::ios::ate);
        if (!file) {
            *error = "failed to open model: " + path;
            return false;
        }
        std::streamsize size = file.tellg();
        file.seekg(0, std::ios::beg);
        data->resize(static_cast<size_t>(size));
        if (!file.read(reinterpret_cast<char*>(data->data()), size)) {
            *error = "failed to read model: " + path;
            return false;
        }
        return true;
    }

    static cv::Mat letterbox_rgb(const cv::Mat& bgr, int dst_w, int dst_h, LetterBox* meta) {
        meta->src_w = bgr.cols;
        meta->src_h = bgr.rows;
        meta->scale = std::min(dst_w / static_cast<float>(bgr.cols), dst_h / static_cast<float>(bgr.rows));
        int resized_w = static_cast<int>(std::round(bgr.cols * meta->scale));
        int resized_h = static_cast<int>(std::round(bgr.rows * meta->scale));
        meta->pad_x = (dst_w - resized_w) / 2;
        meta->pad_y = (dst_h - resized_h) / 2;

        cv::Mat resized;
        cv::resize(bgr, resized, cv::Size(resized_w, resized_h), 0, 0, cv::INTER_LINEAR);

        cv::Mat canvas(dst_h, dst_w, CV_8UC3, cv::Scalar(0, 0, 0));
        resized.copyTo(canvas(cv::Rect(meta->pad_x, meta->pad_y, resized_w, resized_h)));

        cv::Mat rgb;
        cv::cvtColor(canvas, rgb, cv::COLOR_BGR2RGB);
        return rgb;
    }

    static float intersection_over_union(const Detection& a, const Detection& b) {
        float xx1 = std::max(a.x1, b.x1);
        float yy1 = std::max(a.y1, b.y1);
        float xx2 = std::min(a.x2, b.x2);
        float yy2 = std::min(a.y2, b.y2);
        float w = std::max(0.0f, xx2 - xx1);
        float h = std::max(0.0f, yy2 - yy1);
        float inter = w * h;
        float area_a = std::max(0.0f, a.x2 - a.x1) * std::max(0.0f, a.y2 - a.y1);
        float area_b = std::max(0.0f, b.x2 - b.x1) * std::max(0.0f, b.y2 - b.y1);
        float denom = area_a + area_b - inter;
        return denom <= 0.0f ? 0.0f : inter / denom;
    }

    static std::vector<Detection> nms(std::vector<Detection> dets, float iou_thresh, int max_det) {
        std::sort(dets.begin(), dets.end(), [](const Detection& a, const Detection& b) {
            return a.score > b.score;
        });

        std::vector<Detection> kept;
        std::vector<char> removed(dets.size(), 0);
        for (size_t i = 0; i < dets.size(); ++i) {
            if (removed[i]) {
                continue;
            }
            kept.push_back(dets[i]);
            if (max_det > 0 && static_cast<int>(kept.size()) >= max_det) {
                break;
            }
            for (size_t j = i + 1; j < dets.size(); ++j) {
                if (!removed[j] && intersection_over_union(dets[i], dets[j]) > iou_thresh) {
                    removed[j] = 1;
                }
            }
        }
        return kept;
    }

    static float clampf(float v, float lo, float hi) {
        return std::max(lo, std::min(v, hi));
    }

    static std::vector<Detection> parse_yolov8_output(const float* data, int elem_count,
                                                      const rknn_tensor_attr& attr, int input_w, int input_h,
                                                      const LetterBox& meta, float conf_thresh,
                                                      float iou_thresh, int max_det) {
        const int features = 5;
        if (elem_count < features || elem_count % features != 0) {
            return std::vector<Detection>();
        }
        int anchors = elem_count / features;

        bool channel_first = true;
        if (attr.n_dims >= 3) {
            for (uint32_t i = 0; i < attr.n_dims; ++i) {
                if (attr.dims[i] == static_cast<uint32_t>(features)) {
                    channel_first = (i != attr.n_dims - 1);
                    break;
                }
            }
        }

        std::vector<Detection> dets;
        dets.reserve(128);
        for (int i = 0; i < anchors; ++i) {
            float cx;
            float cy;
            float w;
            float h;
            float score;
            if (channel_first) {
                cx = data[0 * anchors + i];
                cy = data[1 * anchors + i];
                w = data[2 * anchors + i];
                h = data[3 * anchors + i];
                score = data[4 * anchors + i];
            } else {
                const float* p = data + i * features;
                cx = p[0];
                cy = p[1];
                w = p[2];
                h = p[3];
                score = p[4];
            }

            if (score < conf_thresh) {
                continue;
            }
            if (std::max(std::max(cx, cy), std::max(w, h)) <= 2.0f) {
                cx *= input_w;
                w *= input_w;
                cy *= input_h;
                h *= input_h;
            }

            float x1 = (cx - w * 0.5f - meta.pad_x) / meta.scale;
            float y1 = (cy - h * 0.5f - meta.pad_y) / meta.scale;
            float x2 = (cx + w * 0.5f - meta.pad_x) / meta.scale;
            float y2 = (cy + h * 0.5f - meta.pad_y) / meta.scale;

            x1 = clampf(x1, 0.0f, static_cast<float>(meta.src_w - 1));
            y1 = clampf(y1, 0.0f, static_cast<float>(meta.src_h - 1));
            x2 = clampf(x2, 0.0f, static_cast<float>(meta.src_w));
            y2 = clampf(y2, 0.0f, static_cast<float>(meta.src_h));
            if (x2 <= x1 || y2 <= y1) {
                continue;
            }
            dets.push_back(Detection{x1, y1, x2, y2, score});
        }
        return nms(dets, iou_thresh, max_det);
    }

    rknn_context ctx_ = 0;
    rknn_input_output_num io_num_;
    rknn_tensor_attr input_attr_;
    rknn_tensor_attr output_attr_;
    rknn_output* outputs_ = NULL;
    int input_w_ = 640;
    int input_h_ = 640;
};

static std::string json_escape(const std::string& text) {
    std::ostringstream out;
    for (unsigned char ch : text) {
        switch (ch) {
            case '\\': out << "\\\\"; break;
            case '"': out << "\\\""; break;
            case '\n': out << "\\n"; break;
            case '\r': out << "\\r"; break;
            case '\t': out << "\\t"; break;
            default:
                if (ch < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", ch);
                    out << buf;
                } else {
                    out << static_cast<char>(ch);
                }
        }
    }
    return out.str();
}

static void write_error(const std::string& error) {
    std::cout << "{\"ok\":false,\"error\":\"" << json_escape(error) << "\"}" << std::endl;
}

static void write_result(const std::vector<Detection>& dets, long elapsed_ms) {
    std::cout << "{\"ok\":true,\"windows\":[";
    for (size_t i = 0; i < dets.size(); ++i) {
        const Detection& d = dets[i];
        if (i > 0) {
            std::cout << ",";
        }
        std::cout << "{\"label\":\"0\",\"score\":" << d.score
                  << ",\"box\":[" << static_cast<int>(std::round(d.x1))
                  << "," << static_cast<int>(std::round(d.y1))
                  << "," << static_cast<int>(std::round(d.x2))
                  << "," << static_cast<int>(std::round(d.y2)) << "]}";
    }
    std::cout << "],\"elapsed_ms\":" << elapsed_ms << ",\"raw_tail\":\"worker\"}" << std::endl;
}

static std::vector<std::string> split_tab(const std::string& line) {
    std::vector<std::string> parts;
    size_t start = 0;
    while (true) {
        size_t pos = line.find('\t', start);
        if (pos == std::string::npos) {
            parts.push_back(line.substr(start));
            break;
        }
        parts.push_back(line.substr(start, pos - start));
        start = pos + 1;
    }
    return parts;
}

int main(int argc, char** argv) {
    if (argc != 2) {
        std::fprintf(stderr, "usage: %s <model.rknn>\n", argv[0]);
        return 2;
    }
    setvbuf(stdout, NULL, _IONBF, 0);
    setvbuf(stderr, NULL, _IONBF, 0);

    WindowYoloWorker worker;
    std::string error;
    if (!worker.init(argv[1], &error)) {
        write_error(error);
        return 1;
    }
    std::cout << "READY" << std::endl;

    std::string line;
    while (std::getline(std::cin, line)) {
        if (line == "QUIT") {
            break;
        }
        if (line.empty()) {
            continue;
        }
        std::vector<std::string> parts = split_tab(line);
        if (parts.empty()) {
            continue;
        }
        float conf = parts.size() > 1 ? std::atof(parts[1].c_str()) : 0.25f;
        float iou = parts.size() > 2 ? std::atof(parts[2].c_str()) : 0.45f;
        int max_det = parts.size() > 3 ? std::atoi(parts[3].c_str()) : 50;

        std::vector<Detection> dets;
        long elapsed_ms = 0;
        error.clear();
        if (!worker.run(parts[0], conf, iou, max_det, &dets, &elapsed_ms, &error)) {
            write_error(error);
            continue;
        }
        write_result(dets, elapsed_ms);
    }
    return 0;
}
