import os, sys
import glob
from ultralytics import YOLO
import csv
from PIL import Image, ImageDraw, ImageFont
import numpy as np
from random import randint
import torch
import torchvision
import argparse
import math
import numpy as np
from skspatial.measurement import area_signed

# um/pixel length
UM_PER_PIXEL = 0.7784
UM_PER_PATCH = UM_PER_PIXEL * 832
#0.5945 µm2/pixel
MAX_DET = 30000
Image.MAX_IMAGE_PIXELS = 1000000000

DEVICE = torch.device('cuda:0')


def scale_boxes(boxes, num_images, img_ind, img_scale):
    boxes[:,(0,2)] = boxes[:,(0,2)] + (img_scale[0]/2)*img_ind[0] # x
    boxes[:,(1,3)] = boxes[:,(1,3)] + (img_scale[1]/2)*img_ind[1] # y
    return boxes
    
def scale_masks(masks, num_images, img_ind, img_scale):
    for m in range(len(masks)):
        masks[m] = masks[m] + (img_scale/2)*img_ind # x
    return masks

# Credit to torchvision/ops/boxes.py
def box_inter_union(boxes1, boxes2):
    area1 = torchvision.ops.box_area(boxes1)
    area2 = torchvision.ops.box_area(boxes2)

    lt = torch.max(boxes1[:, None, :2], boxes2[:, :2])  # [N,M,2]
    rb = torch.min(boxes1[:, None, 2:], boxes2[:, 2:])  # [N,M,2]

    wh = (rb - lt).clamp(min=0)
    inter = wh[:, :, 0] * wh[:, :, 1]  # [N,M]

    union = area1[:, None] + area2 - inter

    return inter, union
    
def local_nms(box_results, mask_results, img_size): # Non-maximum suppression for patch results

    if box_results[1][1].numel() == 0:
        return torch.tensor([], device=DEVICE), []
    
    keep_bool_master = []
    tmp_boxes = []

    tmp_boxes.append(box_results[0][0])
    tmp_boxes.append(box_results[0][1])
    tmp_boxes.append(box_results[0][2])
    tmp_boxes.append(box_results[1][0])

    tmp_boxes.append(box_results[1][2])
    tmp_boxes.append(box_results[2][0])
    tmp_boxes.append(box_results[2][1])
    tmp_boxes.append(box_results[2][2])
    tmp_boxes_torch = torch.cat(tmp_boxes)
    
    # If no neighboring predictions, skip
    if torch.numel(tmp_boxes_torch)==0:
        return torch.tensor([], device=DEVICE), []
    
    for box in box_results[1][1]:
        intersection = box_inter_union(box[:4].unsqueeze(0), tmp_boxes_torch[:,:4])[0]

        surf_area = torchvision.ops.box_area( box[:4].unsqueeze(0) )[0]
        comp = ((surf_area - intersection)/surf_area) < 0.1
        if torch.any( comp ):
            area = torchvision.ops.box_area( tmp_boxes_torch[comp[0],:4] )
            # If boxes overlap significantly, keep larger box
            if (torch.all(surf_area > area) \
                and (box[0] < img_size[0]) \
                and (box[1] < img_size[1])):
                # Also checks that the box starts inside the original image
                keep_bool_master.append( True )
            else:
                keep_bool_master.append( False )
        else:
            keep_bool_master.append( True )
        
        
    if not any(keep_bool_master):
        return torch.tensor([], device=DEVICE), []
    
    box_results = box_results[1][1][keep_bool_master]
    mask_results = [ mask_results[1][1][i] for i in range(len(keep_bool_master)) if keep_bool_master[i] ]
    return box_results, mask_results
    
def inference(model, img, img_filename, size, out_dir):
    
    empty_tensor = torch.tensor([], device=DEVICE)
    
    # divide size of image by size of patch/2
    num_patches = ( np.array(img.size) / (size/2) ).astype(int)
    
    # Run inference on each image
    box_results = []
    mask_results = []

    
    box_results.append( [empty_tensor for _ in range(0, img.size[0], size//2)] )
    box_results[-1] += [empty_tensor, empty_tensor]
    mask_results.append( [[] for _ in range(0, img.size[0], size//2)] )
    mask_results[-1] += [[], []]
    
    patches = [] # For debugging
    
    for y0 in range(0, img.size[1], size//2):
        box_results.append([ empty_tensor ])
        mask_results.append([ [] ])
        for x0 in range(0, img.size[0], size//2):
            
            x1, y1 = x0+size, y0+size
            
            patches += [[x0, y0, x1, y1]] # For visualizing crop grid later
            
            # Create crops (pasting onto blank white image, since the default
            # PIL crop function fills with black, causing false detections):
            img_crop = Image.new('RGB', (size, size), (255, 255, 255))
            img_crop.paste(img, (-x0, -y0))
            
            # # Old cropping function (default black fill caused bad detections):
            # img_crop = img.crop((x0,y0,x1,y1))
            
            # # Save cropped images for debugging:
            # img_crop.save( "{f}/{a}_{b}_{id}".format(f=out_dir, a = str(x0), b = str(y0), id=img_filename) )
            
            yc=math.ceil(y0/(size//2))
            xc=math.ceil(x0/(size//2))
            
            results = model( img_crop, verbose=False, device=DEVICE )
            img_ind = np.array((xc,yc))
            
            # Scale the predictions back to their proper size
            for r in range(len(results)):
                boxes = results[r].boxes.data.clone()
                
                if boxes.numel() != 0: # if osteoclasts detected
                    boxes = scale_boxes(boxes, num_patches, img_ind, (size,size))
                    masks = scale_masks(results[r].masks.xy, num_patches, img_ind, np.array((size,size)))
                    box_results[-1].append( boxes )
                    mask_results[-1].append( masks )
                else:
                    box_results[-1].append( empty_tensor )
                    mask_results[-1].append( [] )
                    
        box_results[-1].append( empty_tensor )
        mask_results[-1].append( [] )
    
    box_results.append( [empty_tensor for _ in range(0, img.size[0], size//2)] )
    box_results[-1] += [empty_tensor, empty_tensor]
    mask_results.append( [[] for _ in range(0, img.size[0], size//2)] )
    mask_results[-1] += [[], []]
    
    objects_found = True if box_results else False
    
    if objects_found:
    
        new_box_results = []
        new_mask_results = []
        for r in range(1,len(box_results)-1):
            for c in range(1,len(box_results[r])-1):
                
                # If empty
                if torch.numel(box_results[r][c])==0:
                    continue
                
                output = local_nms([ b[c-1:c+2] for b in box_results[r-1:r+2] ], [ m[c-1:c+2] for m in mask_results[r-1:r+2] ], img.size)
                
                # print(r, c, output[0], '\n')
                
                new_box_results.append(output[0])
                new_mask_results += output[1]

        # If no osteoclasts are detected in image, this will handle the output
        if len(new_box_results) > 0:
            box_results = torch.cat(new_box_results)
            mask_results = new_mask_results
        else:
            box_results = [0]
            mask_results = new_mask_results

    
    with open("{f}/{id}".format(f=out_dir, id=img_filename[:-4]+".txt"), 'w', newline='') as f:
        writer = csv.writer(f, delimiter=',')
        writer.writerow( ["box_x1","box_y1","box_x2","box_y2","objectness_score","mask_x1","mask_y1","mask_x2","mask_y2","..."] )
        if (len(box_results)) > 1:
            for i in range(len(box_results)):
                writer.writerow( box_results[i].tolist()[:-1] + mask_results[i].flatten().tolist() )
        else:
             return f.write("No osteoclasts detected")

    # Draw boxes on original image
    img1 = ImageDraw.Draw(img, 'RGBA')
    font = ImageFont.load_default()
    
    for i, box in enumerate(box_results):
        box = box[:4].type(torch.int)
        
        # The min/max modifiers seem to help boxes on the edge show up:
        shape = [(max(0, box[0]), min(box[1], img.size[0]-1)), \
                 (max(0, box[2]), min(box[3], img.size[1]-1))]
                 
        img1.rectangle(shape, outline="red", width=3)
        img1.text((box[0], box[1]), str(i + 2), font = font, fill="red")
        # print(mask_results[i].astype(int).flatten().tolist())
        mask = mask_results[i].astype(int).flatten().tolist()
        if len(mask) >= 6:
            color = (randint(0,255),randint(0,255),randint(0,255))
            img1.polygon(mask, fill=color+(125,), outline="blue")
    
    for i, patch in enumerate(patches):
        img1.rectangle([(patch[0], patch[1]), (patch[2], patch[3])], outline="green", width=1)
            
    img.save( "{f}/{id}".format(f=out_dir, id=img_filename) )
    
    if objects_found:
        return [{"boxes":box_results[:,:4], "scores":box_results[:,4], "labels":box_results[:,5].int()}]
    else:
        return [{"boxes":[], "scores":[], "labels":[]}]
    
def count_ocls_from_output(out_dir):
    
    # This script will count each newline for the files in the output directory

    #This will save the output_files to a list from the output directory and only include the txt files
    output_files = glob.glob((out_dir) + "*.txt")

    #To iterate over each file in that output directory
    for file in output_files:
        with open(file, "r") as f: # f is now the object of each file
            as_string = str(f.read())
            split_string = as_string.split("\n")
            count_value = (len(split_string[1:-1]))
            with open("ocl_counts.txt", "a") as file:
                file.write("{id}".format(id=f.name[:-4]) + ": " + str(count_value) + "\n")
            file.close

#Below functions are required for area calculations
def masking_coordinates_to_list(out_dir):

    # This function will count each newline for the files in the output directory

    #This will save the output_files to a list from the output directory and only include the txt files
    output_files = glob.glob((out_dir) + "*.txt")

    dir_list = [] # List will contain each .txt file name containing the masking coordinates. 
    for file in os.listdir(out_dir):
        if file.endswith('.txt'):
            dir_list.append(file)

    length_files_in_dir = (len(dir_list)) # Save how many files are in the directory
    
    counter = 0
    coordinate_dict = {}  # Save each file name as a key to the masking coordinate value
    #To iterate over each file in that output directory
    while len(coordinate_dict) != length_files_in_dir:
        for file in (output_files):
            with open(file, "r") as f: # f is now the object of each file
                as_string = str(f.read())
                split_string = as_string.split("\n") # Split string has each osteoclast masking coordinates in an element of a list.
    
                coordinate_dict[dir_list[counter]] = split_string
                counter += 1
    #print(coordinate_dict)
    return coordinate_dict # The coordinate dict will have each file name as a key and the masking coordinates as a value.
              
def calculate_pixel_area(coordinate_list_as_floats):
    '''This function will create a 2d array of the masking coordinates
    as input for the area_signed function which utilizes the shoelace
    formula to calculate area of an irregular polygon. 

    Returns the pixel area of each osteoclast. '''

    for i in coordinate_list_as_floats:
         if isinstance(i, float):      
            array_2d = np.array(coordinate_list_as_floats).reshape((len(coordinate_list_as_floats))//2,2) #Create numpy array

            pixel_area = (abs(area_signed(array_2d))) # Output Pixel area of each osteoclast using shoelace formula

            return (pixel_area)

def pixel_area_to_um_sqrd(pixel_area, ratio):
     
    '''This function will calculate the area of each ocl in um^2.
    Calculations will be based on ratio given by the user.'''

    # Tie this into the ratio argument, the use will enter a ratio 
    # For pixel area to um^2 at 4X
    '''if magnification == "4":
        um_sqrd_area = pixel_area * ((1.883) ** 2)

    # For pixel area to um^2 at 10X
    elif magnification == "10":
        um_sqrd_area = pixel_area * ((0.75488) ** 2)

    # For pixel area to um^2 at 20X
    elif magnification == "20":
        um_sqrd_area = pixel_area * ((0.40825) ** 2)

    # For pixel area to um^2 at 40X
    elif magnification == "40":
        um_sqrd_area = pixel_area * ((0.401) ** 2)'''

    
    um_sqrd_area = pixel_area * ((ratio) ** 2)
    return round(um_sqrd_area, 3)

def total_area_per_well(area_list):

    # Each area of an ocl will be added to the total area 
    total_area_per_well_sum = 0

    for i in area_list:
         total_area_per_well_sum += i

    return round(total_area_per_well_sum, 3)


def percent_ocl_area_per_well(total_area, well_area_in_pixels):

    '''Function will calculate the percent of osteoclast area on each well.
    The well_area_in_pixels is set to the user entered area.'''
    perecent_area = (total_area/well_area_in_pixels)*100

    return round(perecent_area, 3)

def write_area_to_output(total_area_per_well_sum, percent_area, out_dir,key):

    '''Function will write each area to a txt.'''

    with open("ocl_area.txt", "a") as file:
        file.write(f"{key}" + ": " + " Total area = " + str(total_area_per_well_sum) + ":" + " % area = " + str(percent_area) + "\n")
    file.close


def main(argv):
    
    # move the sys args into parser
    parser = argparse.ArgumentParser()
    parser.add_argument("--img_foldername", type=str, default="img")
    parser.add_argument("--out_foldername", type=str, default="out")
    parser.add_argument("--model_path", type=str, default="out")
    parser.add_argument("--ratio", type=float, default=0.7784) #um per pixel
    parser.add_argument("--device", type=str, default='cpu')
    #parser.add_argument("--magnification", type=str, default="10")
    parser.add_argument("--total_well_area_in_pixels", type = int, default = 0)

    args = parser.parse_args()
    
    um_per_pixel = args.ratio
    patch_size = int( UM_PER_PATCH/um_per_pixel )
    
    out_dir = args.out_foldername
    img_dir = args.img_foldername

    #magnification = args.magnification
    well_area_in_pixels = args.total_well_area_in_pixels

    global DEVICE
    DEVICE = torch.device(args.device)
    
    if out_dir == img_dir:
        print("Error: Input directory equals output directory. Please specify a unique output directory.")
        return
    
    # check if out_dir exists and create if it doesn't
    if not os.path.exists( out_dir ):
        os.makedirs( out_dir )
        
    model_path = args.model_path
    model = YOLO(model_path)
        
    img_files = [ file for file in os.listdir(img_dir) if not file.startswith(".") ]
    for img_filename in img_files:
        print(img_filename)
        
        img = Image.open( os.path.join(img_dir, img_filename) )
        
        pred = inference(model, img, img_filename, patch_size, out_dir)

    
    count_ocls_from_output(out_dir)

    # Below added for area calculations 
    split_string = masking_coordinates_to_list(out_dir) # Split string variable is now a dictionary, where each each key is a txt and value is a set of masking coordinates.
    
    for keys,values in (split_string.items()):
        pixel_area_list = [] # The list containing the pixel area calculated by the shoelace formula
        file_key = []
        for i in range(len(values)):
            if i != 0 and i != (len(values)-1):
                coordinate_list = values[i].split(',') 
                
                coordinate_list_as_floats = [] # New list is now a list of coordinates as a float
                for i in coordinate_list[5:]: # This will exclude the box coordinates and object score
                    if len(coordinate_list[5:]) >= 9: #This will make sure that the area is a three sided polygon. 
                        flt = float(i)
                        coordinate_list_as_floats.append(flt)
                    else:
                        continue

                # This conditional statement will make sure that every coordinate list has floats
                if len(coordinate_list_as_floats) >= 3 :
                    pixel_area = calculate_pixel_area(coordinate_list_as_floats)

                    # This will compute the area in um^2 for each ocl
                    # um_sqrd = pixel_area_to_um_sqrd(pixel_area,magnification)

                pixel_area_list.append(pixel_area)

                  
        #print("The pixel area list is", pixel_area_list, "\n")

        # Below will determine the total area of ocls in each well in um^2.

        # This will calculate total area of ocls in pixels
        total_area = (total_area_per_well(pixel_area_list))

        total_area_in_um_sqrd = pixel_area_to_um_sqrd(total_area, um_per_pixel) # um per pixel is the ratio user argument

        # Used for testing
        #print("The total area is", total_area, "pixels", "\n")

        if well_area_in_pixels != 0:
            percent_ocl_each_well = percent_ocl_area_per_well(total_area, well_area_in_pixels)

            #Used for testing
            #print("The percent of ocl area per well is", str(percent_ocl_each_well) ,"%")
        
        # Need to fix that when no percent ocl area, the area is not written too in output 
            write_area_to_output(total_area_in_um_sqrd, percent_ocl_each_well, out_dir, keys)
        
        else:
            percent_ocl_each_well = "None"
            write_area_to_output(total_area_in_um_sqrd, percent_ocl_each_well, out_dir, keys)

            print("If you want total area calculated, please enter pixel area of each well into the --total_well_area_in_pixels argument.")
            
        print("output written")
    
    # Update 10/22/24: The total area is now output as um sqrd, no longer based off of the magnification but instead the ratio from the user. 

    

    return
    

if __name__ == '__main__':
    main(sys.argv)
