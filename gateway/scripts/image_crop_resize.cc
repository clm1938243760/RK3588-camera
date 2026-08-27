#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <string>

#include <opencv2/opencv.hpp>

static int clamp_int(int value, int low, int high) {
    return std::max(low, std::min(value, high));
}

int main(int argc, char** argv) {
    if (argc < 7 || argc > 9) {
        std::fprintf(stderr, "usage: %s <input> <output> <x1> <y1> <x2> <y2> [margin] [scale]\n", argv[0]);
        return 2;
    }

    const std::string input_path = argv[1];
    const std::string output_path = argv[2];
    int x1 = std::atoi(argv[3]);
    int y1 = std::atoi(argv[4]);
    int x2 = std::atoi(argv[5]);
    int y2 = std::atoi(argv[6]);
    const int margin = argc >= 8 ? std::atoi(argv[7]) : 0;
    const double scale = argc >= 9 ? std::atof(argv[8]) : 1.0;

    cv::Mat image = cv::imread(input_path, cv::IMREAD_COLOR);
    if (image.empty()) {
        std::fprintf(stderr, "failed to read image: %s\n", input_path.c_str());
        return 1;
    }

    x1 = clamp_int(x1 - margin, 0, image.cols - 1);
    y1 = clamp_int(y1 - margin, 0, image.rows - 1);
    x2 = clamp_int(x2 + margin, 0, image.cols);
    y2 = clamp_int(y2 + margin, 0, image.rows);
    if (x2 <= x1 || y2 <= y1) {
        std::fprintf(stderr, "invalid crop box after clamp\n");
        return 1;
    }

    cv::Mat crop = image(cv::Rect(x1, y1, x2 - x1, y2 - y1)).clone();
    if (scale > 1.01) {
        cv::Mat resized;
        cv::resize(crop, resized, cv::Size(), scale, scale, cv::INTER_CUBIC);
        crop = resized;
    }

    std::vector<int> params;
    params.push_back(cv::IMWRITE_JPEG_QUALITY);
    params.push_back(95);
    if (!cv::imwrite(output_path, crop, params)) {
        std::fprintf(stderr, "failed to write image: %s\n", output_path.c_str());
        return 1;
    }

    std::printf("CROP box=%d,%d,%d,%d size=%dx%d scale=%.3f\n", x1, y1, x2, y2, crop.cols, crop.rows, scale);
    return 0;
}
