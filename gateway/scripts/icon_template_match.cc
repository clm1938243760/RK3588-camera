#include <cstdio>
#include <cstdlib>
#include <string>

#include <opencv2/opencv.hpp>

int main(int argc, char** argv) {
    if (argc < 3 || argc > 6) {
        std::fprintf(stderr, "usage: %s <image> <template> [threshold] [offset_x] [offset_y]\n", argv[0]);
        return 2;
    }

    const std::string image_path = argv[1];
    const std::string template_path = argv[2];
    const double threshold = argc >= 4 ? std::atof(argv[3]) : 0.65;
    const int offset_x = argc >= 5 ? std::atoi(argv[4]) : -1;
    const int offset_y = argc >= 6 ? std::atoi(argv[5]) : -1;

    cv::Mat image = cv::imread(image_path, cv::IMREAD_COLOR);
    cv::Mat templ = cv::imread(template_path, cv::IMREAD_COLOR);
    if (image.empty()) {
        std::fprintf(stderr, "failed to read image: %s\n", image_path.c_str());
        return 1;
    }
    if (templ.empty()) {
        std::fprintf(stderr, "failed to read template: %s\n", template_path.c_str());
        return 1;
    }
    if (image.cols < templ.cols || image.rows < templ.rows) {
        std::fprintf(stderr, "template is larger than image\n");
        return 1;
    }

    cv::Mat image_gray;
    cv::Mat templ_gray;
    cv::cvtColor(image, image_gray, cv::COLOR_BGR2GRAY);
    cv::cvtColor(templ, templ_gray, cv::COLOR_BGR2GRAY);

    cv::Mat result;
    cv::matchTemplate(image_gray, templ_gray, result, cv::TM_CCOEFF_NORMED);

    double min_val = 0.0;
    double max_val = 0.0;
    cv::Point min_loc;
    cv::Point max_loc;
    cv::minMaxLoc(result, &min_val, &max_val, &min_loc, &max_loc);

    if (max_val < threshold) {
        std::printf("ICON score=%.6f center=null box=null\n", max_val);
        return 0;
    }

    int cx = max_loc.x + (offset_x >= 0 ? offset_x : templ.cols / 2);
    int cy = max_loc.y + (offset_y >= 0 ? offset_y : templ.rows / 2);
    std::printf(
        "ICON score=%.6f center=%d,%d box=%d,%d,%d,%d\n",
        max_val,
        cx,
        cy,
        max_loc.x,
        max_loc.y,
        max_loc.x + templ.cols,
        max_loc.y + templ.rows
    );
    return 0;
}
