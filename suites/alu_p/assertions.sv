module alu_p_sva(
    input logic clk, input logic rst,
    input logic [7:0] a, input logic [7:0] b, input logic [2:0] op,
    input logic [7:0] y, input logic lt
);
    logic seen_rst = 1'b0;      // outputs unspecified before first reset edge
    logic past_valid = 1'b0;
    always_ff @(posedge clk) begin
        past_valid <= 1'b1;
        if (rst) seen_rst <= 1'b1;
    end

    logic [7:0] exp_y;
    always_comb begin
        case (op)
            3'd0: exp_y = a + b;
            3'd1: exp_y = a - b;
            3'd2: exp_y = a & b;
            3'd3: exp_y = a | b;
            3'd4: exp_y = a ^ b;
            3'd5: exp_y = 8'(a << b[2:0]);
            3'd6: exp_y = a >> b[2:0];
            default: exp_y = 8'd0;
        endcase
    end

    // reset edge clears both outputs
    assert property (@(posedge clk) past_valid && $past(rst) |-> (y == 8'd0 && lt == 1'b0));
    // registered result of previous-cycle inputs
    assert property (@(posedge clk) past_valid && seen_rst && !$past(rst) && $past(seen_rst)
        |-> (y == $past(exp_y) && lt == ($past(a) < $past(b))));
endmodule
