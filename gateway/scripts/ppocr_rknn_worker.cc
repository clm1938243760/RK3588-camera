#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include <opencv2/opencv.hpp>
#include <unistd.h>

#include "image_utils.h"
#include "ppocr_system.h"

extern int release_ppocr_rec_parallel_pool() __attribute__((weak));

#define THRESHOLD 0.3
#define BOX_THRESHOLD 0.6
#define USE_DILATION false
#define DB_SCORE_MODE "slow"
#define DB_BOX_TYPE "poly"
#define DB_UNCLIP_RATIO 1.5

static int saved_stdout_fd = -1;

static void redirect_logs_to_stderr() {
    fflush(stdout);
    if (saved_stdout_fd < 0) {
        saved_stdout_fd = dup(STDOUT_FILENO);
    }
    dup2(STDERR_FILENO, STDOUT_FILENO);
}

static void restore_stdout() {
    fflush(stdout);
    if (saved_stdout_fd >= 0) {
        dup2(saved_stdout_fd, STDOUT_FILENO);
    }
}

static std::string json_escape(const char* text) {
    std::ostringstream out;
    const unsigned char* p = reinterpret_cast<const unsigned char*>(text ? text : "");
    while (*p) {
        unsigned char ch = *p++;
        switch (ch) {
            case '\\': out << "\\\\"; break;
            case '"': out << "\\\""; break;
            case '\b': out << "\\b"; break;
            case '\f': out << "\\f"; break;
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
    restore_stdout();
    std::cout << "{\"ok\":false,\"error\":\"" << json_escape(error.c_str()) << "\"}" << std::endl;
}

static void write_result(image_buffer_t* image, const ppocr_text_recog_array_result_t& results, long elapsed_ms) {
    restore_stdout();
    std::cout << "{\"ok\":true,\"ocr\":[";
    for (int i = 0; i < results.count; ++i) {
        const ppocr_text_recog_result_t& item = results.text_result[i];
        const rknn_quad_t& b = item.box;
        int min_x = std::min(std::min(b.left_top.x, b.right_top.x), std::min(b.right_bottom.x, b.left_bottom.x));
        int min_y = std::min(std::min(b.left_top.y, b.right_top.y), std::min(b.right_bottom.y, b.left_bottom.y));
        int max_x = std::max(std::max(b.left_top.x, b.right_top.x), std::max(b.right_bottom.x, b.left_bottom.x));
        int max_y = std::max(std::max(b.left_top.y, b.right_top.y), std::max(b.right_bottom.y, b.left_bottom.y));
        int cx = (b.left_top.x + b.right_top.x + b.right_bottom.x + b.left_bottom.x) / 4;
        int cy = (b.left_top.y + b.right_top.y + b.right_bottom.y + b.left_bottom.y) / 4;
        if (i > 0) {
            std::cout << ",";
        }
        std::cout
            << "{\"index\":" << i
            << ",\"polygon\":[[" << b.left_top.x << "," << b.left_top.y << "],["
            << b.right_top.x << "," << b.right_top.y << "],["
            << b.right_bottom.x << "," << b.right_bottom.y << "],["
            << b.left_bottom.x << "," << b.left_bottom.y << "]]"
            << ",\"box\":[" << min_x << "," << min_y << "," << max_x << "," << max_y << "]"
            << ",\"center\":[" << cx << "," << cy << "]"
            << ",\"text\":\"" << json_escape(item.text.str) << "\""
            << ",\"score\":" << item.text.score
            << "}";
    }
    std::cout << "],\"image_size\":{\"width\":" << image->width << ",\"height\":" << image->height
              << "},\"elapsed_ms\":" << elapsed_ms << ",\"raw_tail\":\"worker\"}" << std::endl;
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

static int clamp_int(int value, int lo, int hi) {
    return std::max(lo, std::min(value, hi));
}

static bool make_crop_image(const image_buffer_t& src, const std::vector<std::string>& parts, image_buffer_t* crop) {
    if (parts.size() < 5) {
        return false;
    }
    int x1 = std::atoi(parts[1].c_str());
    int y1 = std::atoi(parts[2].c_str());
    int x2 = std::atoi(parts[3].c_str());
    int y2 = std::atoi(parts[4].c_str());
    int margin = parts.size() > 5 ? std::atoi(parts[5].c_str()) : 0;
    float scale = parts.size() > 6 ? std::atof(parts[6].c_str()) : 1.0f;
    if (scale <= 0.0f) {
        scale = 1.0f;
    }

    x1 = clamp_int(x1 - margin, 0, src.width - 1);
    y1 = clamp_int(y1 - margin, 0, src.height - 1);
    x2 = clamp_int(x2 + margin, 0, src.width);
    y2 = clamp_int(y2 + margin, 0, src.height);
    if (x2 <= x1 || y2 <= y1) {
        return false;
    }

    cv::Mat src_mat(src.height, src.width, CV_8UC3, src.virt_addr);
    cv::Mat crop_mat = src_mat(cv::Rect(x1, y1, x2 - x1, y2 - y1)).clone();
    if (scale != 1.0f) {
        cv::Mat resized;
        cv::resize(crop_mat, resized, cv::Size(), scale, scale, cv::INTER_LINEAR);
        crop_mat = resized;
    }

    std::memset(crop, 0, sizeof(*crop));
    crop->width = crop_mat.cols;
    crop->height = crop_mat.rows;
    crop->width_stride = crop_mat.cols;
    crop->height_stride = crop_mat.rows;
    crop->format = IMAGE_FORMAT_RGB888;
    crop->size = static_cast<int>(crop_mat.total() * crop_mat.elemSize());
    crop->virt_addr = static_cast<unsigned char*>(std::malloc(crop->size));
    if (crop->virt_addr == NULL) {
        return false;
    }
    std::memcpy(crop->virt_addr, crop_mat.data, crop->size);
    return true;
}

static int run_one(ppocr_system_app_context* ctx, const std::string& line) {
    std::vector<std::string> parts = split_tab(line);
    if (parts.empty() || parts[0].empty()) {
        write_error("missing image path");
        return -1;
    }
    const std::string& image_path = parts[0];
    image_buffer_t src_image;
    std::memset(&src_image, 0, sizeof(src_image));
    image_buffer_t crop_image;
    std::memset(&crop_image, 0, sizeof(crop_image));
    image_buffer_t* infer_image = &src_image;

    ppocr_det_postprocess_params params;
    params.threshold = THRESHOLD;
    params.box_threshold = BOX_THRESHOLD;
    params.use_dilate = USE_DILATION;
    params.db_score_mode = (char*)DB_SCORE_MODE;
    params.db_box_type = (char*)DB_BOX_TYPE;
    params.db_unclip_ratio = DB_UNCLIP_RATIO;

    auto started = std::chrono::steady_clock::now();
    redirect_logs_to_stderr();
    int ret = read_image(image_path.c_str(), &src_image);
    if (ret != 0) {
        if (src_image.virt_addr) {
            free(src_image.virt_addr);
        }
        write_error("read image failed: " + image_path);
        return -1;
    }

    ppocr_text_recog_array_result_t results;
    std::memset(&results, 0, sizeof(results));
    if (parts.size() >= 5 && make_crop_image(src_image, parts, &crop_image)) {
        infer_image = &crop_image;
    }

    ret = inference_ppocr_system_model(ctx, infer_image, &params, &results);
    auto finished = std::chrono::steady_clock::now();
    long elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(finished - started).count();
    if (ret != 0) {
        if (crop_image.virt_addr) {
            free(crop_image.virt_addr);
        }
        if (src_image.virt_addr) {
            free(src_image.virt_addr);
        }
        write_error("inference failed");
        return -1;
    }

    write_result(infer_image, results, elapsed_ms);
    if (crop_image.virt_addr) {
        free(crop_image.virt_addr);
    }
    if (src_image.virt_addr) {
        free(src_image.virt_addr);
    }
    return 0;
}

int main(int argc, char** argv) {
    if (argc != 3) {
        std::fprintf(stderr, "usage: %s <det_model.rknn> <rec_model.rknn>\n", argv[0]);
        return 2;
    }

    setvbuf(stdout, NULL, _IONBF, 0);
    setvbuf(stderr, NULL, _IONBF, 0);

    ppocr_system_app_context ctx;
    std::memset(&ctx, 0, sizeof(ctx));

    redirect_logs_to_stderr();
    int ret = init_ppocr_model(argv[1], &ctx.det_context);
    if (ret != 0) {
        write_error("init det model failed");
        return 1;
    }
    ret = init_ppocr_model(argv[2], &ctx.rec_context);
    if (ret != 0) {
        release_ppocr_model(&ctx.det_context);
        write_error("init rec model failed");
        return 1;
    }
    restore_stdout();
    std::cout << "READY" << std::endl;

    std::string line;
    while (std::getline(std::cin, line)) {
        if (line == "QUIT") {
            break;
        }
        if (line.empty()) {
            continue;
        }
        run_one(&ctx, line);
    }

    redirect_logs_to_stderr();
    release_ppocr_model(&ctx.det_context);
    if (release_ppocr_rec_parallel_pool != NULL) {
        release_ppocr_rec_parallel_pool();
    }
    release_ppocr_model(&ctx.rec_context);
    restore_stdout();
    if (saved_stdout_fd >= 0) {
        close(saved_stdout_fd);
    }
    return 0;
}
