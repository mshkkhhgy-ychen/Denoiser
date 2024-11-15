import torch
import torch.nn as nn
from timm.models.layers import to_2tuple, trunc_normal_
from .bottleneck import Bottleneck
from .swin_basiclayer import *
from .gc_basiclayer import * 

class CrossAttentionLayer(nn.Module):
    def __init__(self, local_dim, global_dim, num_heads=8):
        super(CrossAttentionLayer, self).__init__()
        self.num_heads = num_heads
        self.local_dim = local_dim
        self.global_dim = global_dim

        # Ensure that local_dim and global_dim are divisible by num_heads
        assert local_dim % num_heads == 0, "local_dim must be divisible by num_heads"
        assert global_dim % num_heads == 0, "global_dim must be divisible by num_heads"

        self.dim_head_local = local_dim // num_heads
        self.dim_head_global = global_dim // num_heads

        # Linear projections for local features (queries)
        self.query_proj = nn.Linear(local_dim, local_dim)

        # Linear projections for global features (keys and values)
        self.key_proj = nn.Linear(global_dim, local_dim)
        self.value_proj = nn.Linear(global_dim, local_dim)

        # Output projection
        self.out_proj = nn.Linear(local_dim, local_dim)

        # Optional: Layer normalization
        self.norm_local = nn.LayerNorm(local_dim)
        self.norm_global = nn.LayerNorm(global_dim)

    def forward(self, x_local, x_global):
        """
        x_local: Tensor of shape (B, L, C_local), where L = H_local * W_local
        x_global: Tensor of shape (B, C_global, H_global, W_global)
        """
        B, L, C_local = x_local.shape
        B_global, C_global, H_global, W_global = x_global.shape

        # Reshape x_global to (B, L_global, C_global)
        x_global = x_global.view(B_global, C_global, -1).transpose(1, 2)  # Shape: (B, L_global, C_global)
        L_global = H_global * W_global

        # Optional: Normalize inputs
        x_local_norm = self.norm_local(x_local)
        x_global_norm = self.norm_global(x_global)

        # Project local features (queries)
        Q = self.query_proj(x_local_norm)  # Shape: (B, L, C_local)

        # Project global features (keys and values)
        K = self.key_proj(x_global_norm)   # Shape: (B, L_global, C_local)
        V = self.value_proj(x_global_norm) # Shape: (B, L_global, C_local)

        # Reshape for multi-head attention
        Q = Q.view(B, L, self.num_heads, self.dim_head_local).transpose(1, 2)  # Shape: (B, num_heads, L, dim_head_local)
        K = K.view(B, L_global, self.num_heads, self.dim_head_local).transpose(1, 2)  # Shape: (B, num_heads, L_global, dim_head_local)
        V = V.view(B, L_global, self.num_heads, self.dim_head_local).transpose(1, 2)  # Shape: (B, num_heads, L_global, dim_head_local)

        # Scaled dot-product attention
        attn_scores = torch.matmul(Q, K.transpose(-2, -1)) / (self.dim_head_local ** 0.5)  # Shape: (B, num_heads, L, L_global)
        attn_weights = torch.softmax(attn_scores, dim=-1)  # Shape: (B, num_heads, L, L_global)

        attn_output = torch.matmul(attn_weights, V)  # Shape: (B, num_heads, L, dim_head_local)

        # Concatenate heads
        attn_output = attn_output.transpose(1, 2).contiguous().view(B, L, C_local)  # Shape: (B, L, C_local)

        # Output projection
        attn_output = self.out_proj(attn_output)  # Shape: (B, L, C_local)

        # Residual connection
        x_merged = x_local + attn_output  # Shape: (B, L, C_local)

        return x_merged

class PatchEmbed(nn.Module):
    r""" Image to Patch Embedding

    Args:
        img_size (int): Image size.  Default: 224.
        patch_size (int): Patch token size. Default: 4.
        in_chans (int): Number of input image channels. Default: 3.
        embed_dim (int): Number of linear projection output channels. Default: 96.
        norm_layer (nn.Module, optional): Normalization layer. Default: None
    """

    def __init__(self, img_size=224, patch_size=4, in_chans=3, embed_dim=96, norm_layer=None):
        super().__init__()
        img_size = to_2tuple(img_size)
        patch_size = to_2tuple(patch_size)
        patches_resolution = [img_size[0] // patch_size[0], img_size[1] // patch_size[1]]
        self.img_size = img_size
        self.patch_size = patch_size
        self.patches_resolution = patches_resolution
        self.num_patches = patches_resolution[0] * patches_resolution[1]

        self.in_chans = in_chans
        self.embed_dim = embed_dim

        self.proj = nn.Conv2d(in_chans, embed_dim, kernel_size=patch_size, stride=patch_size)
        if norm_layer is not None:
            self.norm = norm_layer(embed_dim)
        else:
            self.norm = None

    def forward(self, x):
        B, C, H, W = x.shape
        # FIXME look at relaxing size constraints
        # assert H == self.img_size[0] and W == self.img_size[1], \
        #    f"Input image size ({H}*{W}) doesn't match model ({self.img_size[0]}*{self.img_size[1]})."
        x = self.proj(x).flatten(2).transpose(1, 2)  # B Ph*Pw C
        if self.norm is not None:
            x = self.norm(x)
        return x

    def flops(self):
        Ho, Wo = self.patches_resolution
        flops = Ho * Wo * self.embed_dim * self.in_chans * (self.patch_size[0] * self.patch_size[1])
        if self.norm is not None:
            flops += Ho * Wo * self.embed_dim
        return flops


class SUNet(nn.Module):
    r""" Swin Transformer
        A PyTorch impl of : `Swin Transformer: Hierarchical Vision Transformer using Shifted Windows`  -
          https://arxiv.org/pdf/2103.14030

    Args:
        img_size (int | tuple(int)): Input image size. Default 224
        patch_size (int | tuple(int)): Patch size. Default: 4
        in_chans (int): Number of input image channels. Default: 3

        embed_dim (int): Patch embedding dimension. Default: 96
        depths (tuple(int)): Depth of each Swin Transformer layer.
        num_heads (tuple(int)): Number of attention heads in different layers.
        window_size (int): Window size. Default: 7
        mlp_ratio (float): Ratio of mlp hidden dim to embedding dim. Default: 4
        qkv_bias (bool): If True, add a learnable bias to query, key, value. Default: True
        qk_scale (float): Override default qk scale of head_dim ** -0.5 if set. Default: None
        drop_rate (float): Dropout rate. Default: 0
        attn_drop_rate (float): Attention dropout rate. Default: 0
        drop_path_rate (float): Stochastic depth rate. Default: 0.1
        norm_layer (nn.Module): Normalization layer. Default: nn.LayerNorm.
        ape (bool): If True, add absolute position embedding to the patch embedding. Default: False
        patch_norm (bool): If True, add normalization after patch embedding. Default: True
        use_checkpoint (bool): Whether to use checkpointing to save memory. Default: False
    """

    def __init__(self, img_size=224, patch_size=4, in_chans=3, out_chans=3,
                 embed_dim=96, depths=[2, 2, 2, 2], num_heads=[3, 6, 12, 24],
                 window_size=7, mlp_ratio=4., qkv_bias=True, qk_scale=None,
                 drop_rate=0., attn_drop_rate=0., drop_path_rate=0.1,
                 norm_layer=nn.LayerNorm, ape=False, patch_norm=True,
                 use_checkpoint=False, final_upsample="Dual up-sample", **kwargs):
        super(SUNet, self).__init__()

        self.out_chans = out_chans
        self.num_layers = len(depths)
        self.embed_dim = embed_dim
        self.ape = ape
        self.patch_norm = patch_norm
        self.num_features = int(embed_dim * 2 ** (self.num_layers - 1))
        self.num_features_up = int(embed_dim * 2)
        self.mlp_ratio = mlp_ratio
        self.final_upsample = final_upsample
        self.prelu = nn.PReLU()
        self.conv_first = nn.Conv2d(in_chans, embed_dim, 3, 1, 1)

        # split image into non-overlapping patches
        self.patch_embed = PatchEmbed(
            img_size=img_size, patch_size=patch_size, in_chans=embed_dim, embed_dim=embed_dim,
            norm_layer=norm_layer if self.patch_norm else None)
        num_patches = self.patch_embed.num_patches
        patches_resolution = self.patch_embed.patches_resolution
        self.patches_resolution = patches_resolution

        # absolute position embedding
        if self.ape:
            self.absolute_pos_embed = nn.Parameter(torch.zeros(1, num_patches, embed_dim))
            trunc_normal_(self.absolute_pos_embed, std=.02)

        self.pos_drop = nn.Dropout(p=drop_rate)

        # stochastic depth
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, sum(depths))]  # stochastic depth decay rule

        # build encoder and bottleneck layers
        self.layers = nn.ModuleList()
        self.gc_layers = nn.ModuleList()
        for i_layer in range(self.num_layers):
            layer = BasicLayer(dim=int(embed_dim * 2 ** i_layer),
                               input_resolution=(patches_resolution[0] // (2 ** i_layer),
                                                 patches_resolution[1] // (2 ** i_layer)),
                               depth=depths[i_layer],
                               num_heads=num_heads[i_layer],
                               window_size=window_size,
                               mlp_ratio=self.mlp_ratio,
                               qkv_bias=qkv_bias, qk_scale=qk_scale,
                               drop=drop_rate, attn_drop=attn_drop_rate,
                               drop_path=dpr[sum(depths[:i_layer]):sum(depths[:i_layer + 1])],
                               norm_layer=norm_layer,
                               downsample=PatchMerging if (i_layer < self.num_layers - 1) else None,
                               use_checkpoint=use_checkpoint)
            self.layers.append(layer)

            gc_layer = GlobalContextBasicLayer(
                dim=int(embed_dim * 2 ** i_layer),
                depth=depths[i_layer] // 2, 
                downsample=DownsamplingBlock if (i_layer < self.num_layers - 1) else None,
                use_checkpoint=use_checkpoint
            )

            self.gc_layers.append(gc_layer)

        '''
        self.bottleneck = Bottleneck(
            channels=int(embed_dim * 2 ** (i_layer)), 
            block=BasicLayer(dim=int(embed_dim * 2 ** (i_layer)),
                               input_resolution=(patches_resolution[0] // (2 ** i_layer),
                                                 patches_resolution[1] // (2 ** i_layer)),
                               depth=depths[i_layer],
                               num_heads=num_heads[i_layer],
                               window_size=window_size,
                               mlp_ratio=self.mlp_ratio,
                               qkv_bias=qkv_bias, qk_scale=qk_scale,
                               drop=drop_rate, attn_drop=attn_drop_rate,
                               drop_path=dpr[sum(depths[:i_layer]):sum(depths[:i_layer + 1])],
                               norm_layer=norm_layer,
                               downsample= None,
                               use_checkpoint=use_checkpoint)
        )
        '''

        # build decoder layers
        self.layers_up = nn.ModuleList()
        self.concat_back_dim = nn.ModuleList()
        for i_layer in range(self.num_layers):
            concat_linear = nn.Linear(2 * int(embed_dim * 2 ** (self.num_layers - 1 - i_layer)),
                                      int(embed_dim * 2 ** (
                                              self.num_layers - 1 - i_layer))) if i_layer > 0 else nn.Identity()
            if i_layer == 0:
                layer_up = UpSample(input_resolution=patches_resolution[0] // (2 ** (self.num_layers - 1 - i_layer)),
                                    in_channels=int(embed_dim * 2 ** (self.num_layers - 1 - i_layer)), scale_factor=2)
            else:
                layer_up = BasicLayer_up(dim=int(embed_dim * 2 ** (self.num_layers - 1 - i_layer)),
                                         input_resolution=(
                                             patches_resolution[0] // (2 ** (self.num_layers - 1 - i_layer)),
                                             patches_resolution[1] // (2 ** (self.num_layers - 1 - i_layer))),
                                         depth=depths[(self.num_layers - 1 - i_layer)],
                                         num_heads=num_heads[(self.num_layers - 1 - i_layer)],
                                         window_size=window_size,
                                         mlp_ratio=self.mlp_ratio,
                                         qkv_bias=qkv_bias, qk_scale=qk_scale,
                                         drop=drop_rate, attn_drop=attn_drop_rate,
                                         drop_path=dpr[sum(depths[:(self.num_layers - 1 - i_layer)]):sum(
                                             depths[:(self.num_layers - 1 - i_layer) + 1])],
                                         norm_layer=norm_layer,
                                         upsample=UpSample if (i_layer < self.num_layers - 1) else None,
                                         use_checkpoint=use_checkpoint)
            self.layers_up.append(layer_up)
            self.concat_back_dim.append(concat_linear)

        self.norm = norm_layer(self.num_features)
        self.norm_up = norm_layer(self.embed_dim)

        if self.final_upsample == "Dual up-sample":
            self.up = UpSample(input_resolution=(img_size // patch_size, img_size // patch_size),
                               in_channels=embed_dim, scale_factor=4)
            self.output = nn.Conv2d(in_channels=embed_dim, out_channels=self.out_chans, kernel_size=3, stride=1,
                                    padding=1, bias=False)  # kernel = 1

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'absolute_pos_embed'}

    @torch.jit.ignore
    def no_weight_decay_keywords(self):
        return {'relative_position_bias_table'}

    # Encoder and Bottleneck
    def forward_features(self, x):
        residual = x
        x = self.patch_embed(x)
        if self.ape:
            x = x + self.absolute_pos_embed
        x = self.pos_drop(x)
        x_downsample = []

        for layer in self.layers:
            x_downsample.append(x)
            x = layer(x)

        x = self.norm(x)  # B L C

        return x, residual, x_downsample

    # Dencoder and Skip connection
    def forward_up_features(self, x, x_downsample):
        for inx, layer_up in enumerate(self.layers_up):
            if inx == 0:
                x = layer_up(x)
            else:
                x = torch.cat([x, x_downsample[3 - inx]], -1)  # concat last dimension
                x = self.concat_back_dim[inx](x)
                x = layer_up(x)

        x = self.norm_up(x)  # B L C

        return x

    def up_x4(self, x):
        H, W = self.patches_resolution
        B, L, C = x.shape
        assert L == H * W, "input features has wrong size"

        if self.final_upsample == "Dual up-sample":
            x = self.up(x)
            # x = x.view(B, 4 * H, 4 * W, -1)
            x = x.permute(0, 3, 1, 2)  # B,C,H,W

        return x

    def forward(self, x):
        #print(f'initial {x.shape}')

        x = self.conv_first(x) # obtain first feature map

        #print(f'after frist conv {x.shape}')
        x, residual, x_downsample = self.forward_features(x)

        # add downsampling path for gc net 
        # assume gc_x, gc_residual, gc_downsample = self.gc_forward_reatures(x)
        # go through bottle neck 
        # go through forward up features
        # assume gc_up = self.gc_foward_up_features(gc_x, gc_downsample)
        # x_up = self.forward_up_features(x, x_downsample)
        # note that the forward functions should also be recorded like x_downsample so the
        # merging can still happen at the upsampling stage 
        # now, perform merging
        # merge_x, merge_downsample = self.merge_down(x, x_downsample, gc_x, gc_downsample)
        # merge_x = self.merge_up_features(merge_x, merge_downsample, gc_up, x_up) 
        # the merging will then perform some sort of merging logic for which takes care 
        # of the merging

        # note that the merging can be any sort of merging
        # eg. using gc as a conditional supplemental feature for patched sample 
        # cross attention, or read into DiT blocks for conditional see if we can do 
        # something similar s

        # x = self.bottleneck(x)
        x = self.forward_up_features(x, x_downsample)
        x = self.up_x4(x)
        out = self.output(x)
        # x = x + residual
        return out

    def flops(self):
        flops = 0
        flops += self.patch_embed.flops()
        for i, layer in enumerate(self.layers):
            flops += layer.flops()
        flops += self.num_features * self.patches_resolution[0] * self.patches_resolution[1] // (2 ** self.num_layers)
        flops += self.num_features * self.out_chans
        return flops
