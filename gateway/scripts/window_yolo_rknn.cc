#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

#include <opencv2/opencv.hpp>

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

static std::vector<unsigned char> read_file(const std::string& path) {
    std::ifstream file(path.c_str(), std::ios::binary | std::ios::ate);
    if (!file) {
        throw std::runtime_error("failed to open model: " + path);
    }
    std::streamsize size = file.tellg();
    file.seekg(0, std::ios::beg);
    std::vector<unsigned char> data(static_cast<size_t>(size));
    if (!file.read(reinterpret_cast<char*>(data.data()), size)) {
        throw std::runtime_error("failed to read model: " + path);
    }
    return data;
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
    if (denom <= 0.0f) {
        return 0.0f;
    }
    return inter / denom;
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

static std::vector<Detection> parse_yolov8_output(
    const float* data,
    int elem_count,
    const rknn_tensor_attr& attr,
    int input_w,
    int input_h,
    const LetterBox& meta,
    float conf_thresh,
    float iou_thresh,
    int max_det
) {
    const int features = 5;
    if (elem_count < features || elem_count % features != 0) {
        throw std::runtime_error("unexpected YOLO output element count");
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

int main(int argc, char** argv) {
    if (argc < 3 || argc > 6) {
        std::fprintf(stderr, "usage: %s <model.rknn> <image> [conf] [iou] [max_det]\n", argv[0]);
        return 2;
    }

    const std::string model_path = argv[1];
    const std::string image_path = argv[2];
    const float conf_thresh = argc >= 4 ? std::atof(argv[3]) : 0.25f;
    const float iou_thresh = argc >= 5 ? std::atof(argv[4]) : 0.45f;
    const int max_det = argc >= 6 ? std::atoi(argv[5]) : 50;

    rknn_context ctx = 0;
    rknn_output* outputs = NULL;
    try {
        std::vector<unsigned char> model_data = read_file(model_path);
        int ret = rknn_init(&ctx, model_data.data(), static_cast<uint32_t>(model_data.size()), 0, NULL);
        if (ret != RKNN_SUCC) {
            std::fprintf(stderr, "rknn_init failed: %d\n", ret);
            return 1;
        }

        rknn_input_output_num io_num;
        std::memset(&io_num, 0, sizeof(io_num));
        ret = rknn_query(ctx, RKNN_QUERY_IN_OUT_NUM, &io_num, sizeof(io_num));
        if (ret != RKNN_SUCC || io_num.n_input < 1 || io_num.n_output < 1) {
            std::fprintf(stderr, "rknn_query io num failed: %d\n", ret);
            return 1;
        }

        rknn_tensor_attr input_attr;
        std::memset(&input_attr, 0, sizeof(input_attr));
        input_attr.index = 0;
        ret = rknn_query(ctx, RKNN_QUERY_INPUT_ATTR, &input_attr, sizeof(input_attr));
        if (ret != RKNN_SUCC) {
            std::fprintf(stderr, "rknn_query input attr failed: %d\n", ret);
            return 1;
        }

        int input_w = 640;
        int input_h = 640;
        if (input_attr.n_dims == 4) {
            if (input_attr.fmt == RKNN_TENSOR_NHWC) {
                input_h = static_cast<int>(input_attr.dims[1]);
                input_w = static_cast<int>(input_attr.dims[2]);
            } else {
                input_h = static_cast<int>(input_attr.dims[2]);
                input_w = static_cast<int>(input_attr.dims[3]);
            }
        }

        cv::Mat bgr = cv::imread(image_path, cv::IMREAD_COLOR);
        if (bgr.empty()) {
            std::fprintf(stderr, "failed to read image: %s\n", image_path.c_str());
            return 1;
        }
        LetterBox meta;
        cv::Mat rgb = letterbox_rgb(bgr, input_w, input_h, &meta);

        rknn_input input;
        std::memset(&input, 0, sizeof(input));
        input.index = 0;
        input.buf = rgb.data;
        input.size = static_cast<uint32_t>(input_w * input_h * 3);
        input.pass_through = 0;
        input.type = RKNN_TENSOR_UINT8;
        input.fmt = RKNN_TENSOR_NHWC;

        ret = rknn_inputs_set(ctx, 1, &input);
        if (ret != RKNN_SUCC) {
            std::fprintf(stderr, "rknn_inputs_set failed: %d\n", ret);
            return 1;
        }

        ret = rknn_run(ctx, NULL);
        if (ret != RKNN_SUCC) {
            std::fprintf(stderr, "rknn_run failed: %d\n", ret);
            return 1;
        }

        std::vector<rknn_tensor_attr> output_attrs(io_num.n_output);
        outputs = static_cast<rknn_output*>(std::calloc(io_num.n_output, sizeof(rknn_output)));
        if (!outputs) {
            throw std::runtime_error("failed to allocate outputs");
        }
        for (uint32_t i = 0; i < io_num.n_output; ++i) {
            std::memset(&output_attrs[i], 0, sizeof(rknn_tensor_attr));
            output_attrs[i].index = i;
            ret = rknn_query(ctx, RKNN_QUERY_OUTPUT_ATTR, &output_attrs[i], sizeof(rknn_tensor_attr));
            if (ret != RKNN_SUCC) {
                std::fprintf(stderr, "rknn_query output attr failed: %d\n", ret);
                return 1;
            }
            outputs[i].index = i;
            outputs[i].want_float = 1;
            outputs[i].is_prealloc = 0;
        }

        ret = rknn_outputs_get(ctx, io_num.n_output, outputs, NULL);
        if (ret != RKNN_SUCC) {
            std::fprintf(stderr, "rknn_outputs_get failed: %d\n", ret);
            return 1;
        }

        if (io_num.n_output != 1) {
            std::fprintf(stderr, "unexpected YOLO output count: %u\n", io_num.n_output);
            return 1;
        }

        const float* data = static_cast<const float*>(outputs[0].buf);
        int elem_count = static_cast<int>(output_attrs[0].n_elems);
        std::vector<Detection> dets = parse_yolov8_output(
            data, elem_count, output_attrs[0], input_w, input_h, meta, conf_thresh, iou_thresh, max_det);

        for (size_t i = 0; i < dets.size(); ++i) {
            const Detection& d = dets[i];
            std::printf("WINDOW label=0 score=%.6f box=%d,%d,%d,%d\n",
                        d.score,
                        static_cast<int>(std::round(d.x1)),
                        static_cast<int>(std::round(d.y1)),
                        static_cast<int>(std::round(d.x2)),
                        static_cast<int>(std::round(d.y2)));
        }

        rknn_outputs_release(ctx, io_num.n_output, outputs);
        std::free(outputs);
        outputs = NULL;
        rknn_destroy(ctx);
        return 0;
    } catch (const std::exception& exc) {
        std::fprintf(stderr, "error: %s\n", exc.what());
        if (outputs) {
            rknn_outputs_release(ctx, 1, outputs);
            std::free(outputs);
        }
        if (ctx) {
            rknn_destroy(ctx);
        }
        return 1;
    }
}
