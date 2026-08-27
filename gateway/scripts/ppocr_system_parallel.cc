#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <thread>
#include <vector>

#include "opencv2/opencv.hpp"
#include "ppocr_system.h"
#include "common.h"
#include "file_utils.h"
#include "image_utils.h"

bool CompareBox(const std::array<int, 8>& result1, const std::array<int, 8>& result2)
{
    if (result1[1] < result2[1])
    {
        return true;
    } else if (result1[1] == result2[1])
    {
        return result1[0] < result2[0];
    } else
    {
        return false;
    }
}

void SortBoxes(std::vector<std::array<int, 8>>* boxes)
{
    std::sort(boxes->begin(), boxes->end(), CompareBox);

    if (boxes->size() == 0)
    {
        return;
    }

    for (int i = 0; i < boxes->size() - 1; i++) {
        for (int j = i; j >=0 ; j--){
            if (std::abs((*boxes)[j + 1][1] - (*boxes)[j][1]) < 10 && ((*boxes)[j + 1][0] < (*boxes)[j][0]))
            {
                std::swap((*boxes)[i], (*boxes)[i + 1]);
            }
        }
    }

}

cv::Mat GetRotateCropImage(const cv::Mat& srcimage, const std::array<int, 8>& box)
{
    cv::Mat image;
    srcimage.copyTo(image);

    std::vector<std::vector<int>> points;

    for (int i = 0; i < 4; ++i) {
        std::vector<int> tmp;
        tmp.push_back(box[2 * i]);
        tmp.push_back(box[2 * i + 1]);
        points.push_back(tmp);
    }
    int x_collect[4] = {box[0], box[2], box[4], box[6]};
    int y_collect[4] = {box[1], box[3], box[5], box[7]};
    int left = int(*std::min_element(x_collect, x_collect + 4));
    int right = int(*std::max_element(x_collect, x_collect + 4));
    int top = int(*std::min_element(y_collect, y_collect + 4));
    int bottom = int(*std::max_element(y_collect, y_collect + 4));

    cv::Mat img_crop;
    image(cv::Rect(left, top, right - left, bottom - top)).copyTo(img_crop);

    for (int i = 0; i < points.size(); i++) {
        points[i][0] -= left;
        points[i][1] -= top;
    }

    int img_crop_width = int(sqrt(pow(points[0][0] - points[1][0], 2) +
                                    pow(points[0][1] - points[1][1], 2)));
    int img_crop_height = int(sqrt(pow(points[0][0] - points[3][0], 2) +
                                    pow(points[0][1] - points[3][1], 2)));

    cv::Point2f pts_std[4];
    pts_std[0] = cv::Point2f(0., 0.);
    pts_std[1] = cv::Point2f(img_crop_width, 0.);
    pts_std[2] = cv::Point2f(img_crop_width, img_crop_height);
    pts_std[3] = cv::Point2f(0.f, img_crop_height);

    cv::Point2f pointsf[4];
    pointsf[0] = cv::Point2f(points[0][0], points[0][1]);
    pointsf[1] = cv::Point2f(points[1][0], points[1][1]);
    pointsf[2] = cv::Point2f(points[2][0], points[2][1]);
    pointsf[3] = cv::Point2f(points[3][0], points[3][1]);

    cv::Mat M = cv::getPerspectiveTransform(pointsf, pts_std);

    cv::Mat dst_img;
    cv::warpPerspective(img_crop, dst_img, M,
                        cv::Size(img_crop_width, img_crop_height),
                        cv::BORDER_REPLICATE);

    if (float(dst_img.rows) >= float(dst_img.cols) * 1.5) {
        cv::Mat srcCopy = cv::Mat(dst_img.rows, dst_img.cols, dst_img.depth());
        cv::transpose(dst_img, srcCopy);
        cv::flip(srcCopy, srcCopy, 0);
        return srcCopy;
    } else {
        return dst_img;
    }
}

static void dump_tensor_attr(rknn_tensor_attr* attr)
{
    printf("  index=%d, name=%s, n_dims=%d, dims=[%d, %d, %d, %d], n_elems=%d, size=%d, fmt=%s, type=%s, qnt_type=%s, "
            "zp=%d, scale=%f\n",
            attr->index, attr->name, attr->n_dims, attr->dims[0], attr->dims[1], attr->dims[2], attr->dims[3],
            attr->n_elems, attr->size, get_format_string(attr->fmt), get_type_string(attr->type),
            get_qnt_type_string(attr->qnt_type), attr->zp, attr->scale);
}

int init_ppocr_model(const char* model_path, rknn_app_context_t* app_ctx)
{
    int ret;
    int model_len = 0;
    char* model;
    rknn_context ctx = 0;

    // Load RKNN Model
    model_len = read_data_from_file(model_path, &model);
    if (model == NULL) {
        printf("load_model fail!\n");
        return -1;
    }

    ret = rknn_init(&ctx, model, model_len, 0, NULL);
    free(model);
    if (ret < 0) {
        printf("rknn_init fail! ret=%d\n", ret);
        return -1;
    }

    // Get Model Input Output Number
    rknn_input_output_num io_num;
    ret = rknn_query(ctx, RKNN_QUERY_IN_OUT_NUM, &io_num, sizeof(io_num));
    if (ret != RKNN_SUCC) {
        printf("rknn_query fail! ret=%d\n", ret);
        return -1;
    }
    printf("model input num: %d, output num: %d\n", io_num.n_input, io_num.n_output);

    // Get Model Input Info
    printf("input tensors:\n");
    rknn_tensor_attr input_attrs[io_num.n_input];
    memset(input_attrs, 0, sizeof(input_attrs));
    for (int i = 0; i < io_num.n_input; i++) {
        input_attrs[i].index = i;
        ret = rknn_query(ctx, RKNN_QUERY_INPUT_ATTR, &(input_attrs[i]), sizeof(rknn_tensor_attr));
        if (ret != RKNN_SUCC) {
            printf("rknn_query fail! ret=%d\n", ret);
            return -1;
        }
        dump_tensor_attr(&(input_attrs[i]));
    }

    // Get Model Output Info
    printf("output tensors:\n");
    rknn_tensor_attr output_attrs[io_num.n_output];
    memset(output_attrs, 0, sizeof(output_attrs));
    for (int i = 0; i < io_num.n_output; i++) {
        output_attrs[i].index = i;
        ret = rknn_query(ctx, RKNN_QUERY_OUTPUT_ATTR, &(output_attrs[i]), sizeof(rknn_tensor_attr));
        if (ret != RKNN_SUCC) {
            printf("rknn_query fail! ret=%d\n", ret);
            return -1;
        }
        dump_tensor_attr(&(output_attrs[i]));
    }

    // Set to context
    app_ctx->rknn_ctx = ctx;
    app_ctx->io_num = io_num;
    app_ctx->input_attrs = (rknn_tensor_attr*)malloc(io_num.n_input * sizeof(rknn_tensor_attr));
    memcpy(app_ctx->input_attrs, input_attrs, io_num.n_input * sizeof(rknn_tensor_attr));
    app_ctx->output_attrs = (rknn_tensor_attr*)malloc(io_num.n_output * sizeof(rknn_tensor_attr));
    memcpy(app_ctx->output_attrs, output_attrs, io_num.n_output * sizeof(rknn_tensor_attr));

    if (input_attrs[0].fmt == RKNN_TENSOR_NCHW) {
        printf("model is NCHW input fmt\n");
        app_ctx->model_channel = input_attrs[0].dims[1];
        app_ctx->model_height  = input_attrs[0].dims[2];
        app_ctx->model_width   = input_attrs[0].dims[3];
    } else {
        printf("model is NHWC input fmt\n");
        app_ctx->model_height  = input_attrs[0].dims[1];
        app_ctx->model_width   = input_attrs[0].dims[2];
        app_ctx->model_channel = input_attrs[0].dims[3];
    }
    printf("model input height=%d, width=%d, channel=%d\n",
        app_ctx->model_height, app_ctx->model_width, app_ctx->model_channel);

    return 0;
}

int release_ppocr_model(rknn_app_context_t* app_ctx)
{
    if (app_ctx->input_attrs != NULL) {
        free(app_ctx->input_attrs);
        app_ctx->input_attrs = NULL;
    }
    if (app_ctx->output_attrs != NULL) {
        free(app_ctx->output_attrs);
        app_ctx->output_attrs = NULL;
    }
    if (app_ctx->rknn_ctx != 0) {
        rknn_destroy(app_ctx->rknn_ctx);
        app_ctx->rknn_ctx = 0;
    }
    return 0;
}

int inference_ppocr_det_model(rknn_app_context_t* app_ctx, image_buffer_t* src_img, ppocr_det_postprocess_params* params, ppocr_det_result* out_result)
{
    int ret;
    image_buffer_t img;
    rknn_input inputs[1];
    rknn_output outputs[1];

    memset(&img, 0, sizeof(image_buffer_t));
    memset(inputs, 0, sizeof(inputs));
    memset(outputs, 0, sizeof(outputs));

    // Pre Process
    img.width = app_ctx->model_width;
    img.height = app_ctx->model_height;
    img.format = IMAGE_FORMAT_RGB888;
    img.size = get_image_size(&img);
    img.virt_addr = (unsigned char*)malloc(img.size);
    if (img.virt_addr == NULL) {
        printf("malloc buffer size:%d fail!\n", img.size);
        return -1;
    }

    ret = convert_image(src_img, &img, NULL, NULL, 0);
    if (ret < 0) {
        printf("convert_image fail! ret=%d\n", ret);
        return -1;
    }

    // Set Input Data
    inputs[0].index = 0;
    inputs[0].type  = RKNN_TENSOR_UINT8;
    inputs[0].fmt   = RKNN_TENSOR_NHWC;
    inputs[0].size  = app_ctx->model_width * app_ctx->model_height * app_ctx->model_channel;
    inputs[0].buf   = img.virt_addr;

    float scale_w = (float)src_img->width / (float)img.width;
    float scale_h = (float)src_img->height / (float)img.height;

    ret = rknn_inputs_set(app_ctx->rknn_ctx, 1, inputs);
    if (ret < 0) {
        printf("rknn_input_set fail! ret=%d\n", ret);
        return -1;
    }

    // Run
    // printf("rknn_run\n");
    ret = rknn_run(app_ctx->rknn_ctx, nullptr);
    if (ret < 0) {
        printf("rknn_run fail! ret=%d\n", ret);
        return -1;
    }

    // Get Output
    outputs[0].want_float = 1;
    ret = rknn_outputs_get(app_ctx->rknn_ctx, 1, outputs, NULL);
    if (ret < 0) {
        printf("rknn_outputs_get fail! ret=%d\n", ret);
        goto out;
    }

    // Post Process
    ret = dbnet_postprocess((float*)outputs[0].buf, app_ctx->model_width, app_ctx->model_height,
                                                params->threshold, params->box_threshold, params->use_dilate, params->db_score_mode,
                                                params->db_unclip_ratio, params->db_box_type,
                                                scale_w, scale_h, out_result);

    // Remeber to release rknn output
    rknn_outputs_release(app_ctx->rknn_ctx, 1, outputs);

out:
    if (img.virt_addr != NULL) {
        free(img.virt_addr);
    }

    return ret;
}

int inference_ppocr_rec_model(rknn_app_context_t* app_ctx, image_buffer_t* src_img, ppocr_rec_result* out_result)
{
    int ret;
    rknn_input inputs[1];
    rknn_output outputs[1];
    int allow_slight_change = 1;

    memset(inputs, 0, sizeof(inputs));
    memset(outputs, 0, sizeof(outputs));

    // Pre Process
    float ratio = src_img->width / float(src_img->height);
    int resized_w;
    int imgW = app_ctx->model_width, imgH = app_ctx->model_height;
    if (std::ceil(imgH*ratio) > imgW) {
        resized_w = imgW;
    }
    else {
        resized_w = std::ceil(imgH*ratio);
    }

    cv::Mat img_M = cv::Mat(src_img->height, src_img->width, CV_8UC3,(uint8_t*)src_img->virt_addr);
    cv::resize(img_M, img_M, cv::Size(resized_w, imgH));
    img_M.convertTo(img_M, CV_32FC3);
    img_M = (img_M - 127.5)/127.5;
    if (resized_w < imgW) {
        copyMakeBorder(img_M, img_M, 0, 0, 0, imgW- resized_w, cv::BORDER_CONSTANT, 0);
    }

    // Set Input Data
    inputs[0].index = 0;
    inputs[0].type  = RKNN_TENSOR_FLOAT32;
    inputs[0].fmt   = RKNN_TENSOR_NHWC;
    inputs[0].size  = app_ctx->model_width * app_ctx->model_height * app_ctx->model_channel * sizeof(float);
    // inputs[0].buf   = img.virt_addr;
    inputs[0].buf = malloc(inputs[0].size);
    memcpy(inputs[0].buf, img_M.data, inputs[0].size);

    ret = rknn_inputs_set(app_ctx->rknn_ctx, 1, inputs);
    if (ret < 0) {
        printf("rknn_input_set fail! ret=%d\n", ret);
        return -1;
    }

    // Run
    // printf("rknn_run\n");
    ret = rknn_run(app_ctx->rknn_ctx, nullptr);
    if (ret < 0) {
        printf("rknn_run fail! ret=%d\n", ret);
        return -1;
    }

    // Get Output
    int out_len_seq = app_ctx->model_width / 8;
    outputs[0].want_float = 1;
    ret = rknn_outputs_get(app_ctx->rknn_ctx, 1, outputs, NULL);
    if (ret < 0) {
        printf("rknn_outputs_get fail! ret=%d\n", ret);
        goto out;
    }

    // Post Process
    ret = rec_postprocess((float*)outputs[0].buf, MODEL_OUT_CHANNEL, out_len_seq, out_result);

    // Remeber to release rknn output
    rknn_outputs_release(app_ctx->rknn_ctx, 1, outputs);

out:
    if (inputs[0].buf != NULL) {
        free(inputs[0].buf);
    }

    return ret;
}

static std::vector<rknn_context> rec_context_pool;

static int ensure_rec_context_pool(rknn_app_context_t* app_ctx)
{
    if (!rec_context_pool.empty()) {
        return static_cast<int>(rec_context_pool.size());
    }

    const rknn_core_mask masks[] = {
        RKNN_NPU_CORE_0,
        RKNN_NPU_CORE_1,
        RKNN_NPU_CORE_2,
    };
    int ret = rknn_set_core_mask(app_ctx->rknn_ctx, masks[0]);
    if (ret < 0) {
        printf("rknn_set_core_mask core0 failed, using one default context: %d\n", ret);
        rec_context_pool.push_back(app_ctx->rknn_ctx);
        return 1;
    }
    rec_context_pool.push_back(app_ctx->rknn_ctx);

    for (int index = 1; index < 3; ++index) {
        rknn_context duplicate = 0;
        ret = rknn_dup_context(&app_ctx->rknn_ctx, &duplicate);
        if (ret < 0) {
            printf("rknn_dup_context core%d failed: %d\n", index, ret);
            break;
        }
        ret = rknn_set_core_mask(duplicate, masks[index]);
        if (ret < 0) {
            printf("rknn_set_core_mask core%d failed: %d\n", index, ret);
            rknn_destroy(duplicate);
            break;
        }
        rec_context_pool.push_back(duplicate);
    }
    return static_cast<int>(rec_context_pool.size());
}

static int inference_ppocr_rec_on_context(rknn_app_context_t* app_ctx,
                                          rknn_context context,
                                          image_buffer_t* src_img,
                                          ppocr_rec_result* out_result)
{
    rknn_app_context_t view = *app_ctx;
    view.rknn_ctx = context;
    return inference_ppocr_rec_model(&view, src_img, out_result);
}

int release_ppocr_rec_parallel_pool()
{
    for (size_t index = 1; index < rec_context_pool.size(); ++index) {
        rknn_destroy(rec_context_pool[index]);
    }
    rec_context_pool.clear();
    return 0;
}

int inference_ppocr_system_model(ppocr_system_app_context* sys_app_ctx, image_buffer_t* src_img, ppocr_det_postprocess_params* params, ppocr_text_recog_array_result_t* out_result)
{
    int ret;
    // Detect Text
    ppocr_det_result det_results;
    ret = inference_ppocr_det_model(&sys_app_ctx->det_context, src_img, params, &det_results);
    if (ret != 0) {
        printf("inference_ppocr_det_model fail! ret=%d\n", ret);
        return -1;
    }

    // Recogize Text
    out_result->count = 0;
    if (det_results.count == 0) {           // detect nothing
        return 0;
    }

    // boxes to boxes_result
    std::vector<std::array<int, 8>> boxes_result;
    for (int i=0; i < det_results.count; i++) {
        std::array<int, 8> new_box;
        new_box[0] = det_results.box[i].left_top.x;
        new_box[1] = det_results.box[i].left_top.y;
        new_box[2] = det_results.box[i].right_top.x;
        new_box[3] = det_results.box[i].right_top.y;
        new_box[4] = det_results.box[i].right_bottom.x;
        new_box[5] = det_results.box[i].right_bottom.y;
        new_box[6] = det_results.box[i].left_bottom.x;
        new_box[7] = det_results.box[i].left_bottom.y;
        boxes_result.emplace_back(new_box);
    }

    // Sort text boxes in order from top to bottom, left to right for speeding up
    SortBoxes(&boxes_result);

    const int context_count = ensure_rec_context_pool(&sys_app_ctx->rec_context);
    if (context_count < 1) {
        return -1;
    }
    cv::Mat in_image(src_img->height, src_img->width, CV_8UC3, src_img->virt_addr);
    for (size_t first = 0; first < boxes_result.size(); first += context_count) {
        const int image_count = static_cast<int>(
            std::min(static_cast<size_t>(context_count), boxes_result.size() - first));
        std::vector<image_buffer_t> text_images(image_count);
        std::vector<ppocr_rec_result> text_results(image_count);
        std::vector<int> recognition_results(image_count, -1);
        std::vector<std::thread> recognition_threads;

        for (int offset = 0; offset < image_count; ++offset) {
            const size_t index = first + offset;
            cv::Mat crop_image = GetRotateCropImage(in_image, boxes_result[index]);
            image_buffer_t* text_img = &text_images[offset];
            memset(text_img, 0, sizeof(*text_img));
            text_img->width = crop_image.cols;
            text_img->height = crop_image.rows;
            text_img->format = IMAGE_FORMAT_RGB888;
            text_img->size = get_image_size(text_img);
            text_img->virt_addr = static_cast<unsigned char*>(malloc(text_img->size));
            if (text_img->virt_addr == NULL) {
                for (int release = 0; release <= offset; ++release) {
                    free(text_images[release].virt_addr);
                }
                printf("malloc buffer size:%d fail!\n", text_img->size);
                return -1;
            }
            memcpy(text_img->virt_addr, crop_image.data, text_img->size);
        }

        for (int offset = 0; offset < image_count; ++offset) {
            recognition_threads.emplace_back([&, offset]() {
                recognition_results[offset] = inference_ppocr_rec_on_context(
                    &sys_app_ctx->rec_context,
                    rec_context_pool[offset],
                    &text_images[offset],
                    &text_results[offset]);
            });
        }
        for (std::thread& thread : recognition_threads) {
            thread.join();
        }
        for (int offset = 0; offset < image_count; ++offset) {
            free(text_images[offset].virt_addr);
        }
        for (int offset = 0; offset < image_count; ++offset) {
            if (recognition_results[offset] != 0) {
                printf("parallel inference_ppocr_rec_model fail! ret=%d\n",
                       recognition_results[offset]);
                return -1;
            }
        }

        for (int offset = 0; offset < image_count; ++offset) {
            const size_t index = first + offset;
            const ppocr_rec_result& text_result = text_results[offset];
            if (text_result.score < TEXT_SCORE) {
                continue;
            }
            out_result->text_result[out_result->count].box.left_top.x = boxes_result[index][0];
            out_result->text_result[out_result->count].box.left_top.y = boxes_result[index][1];
            out_result->text_result[out_result->count].box.right_top.x = boxes_result[index][2];
            out_result->text_result[out_result->count].box.right_top.y = boxes_result[index][3];
            out_result->text_result[out_result->count].box.right_bottom.x = boxes_result[index][4];
            out_result->text_result[out_result->count].box.right_bottom.y = boxes_result[index][5];
            out_result->text_result[out_result->count].box.left_bottom.x = boxes_result[index][6];
            out_result->text_result[out_result->count].box.left_bottom.y = boxes_result[index][7];
            out_result->text_result[out_result->count].text = text_result;
            out_result->count++;
        }
    }

    return ret;
}
